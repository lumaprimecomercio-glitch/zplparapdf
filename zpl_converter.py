#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
================================================================================
ZPL PARA PDF - AUTOMACAO DE IMPRESSAO EM LOTE
================================================================================
Script Python para conversao automatica de arquivos ZPL em PDF.

Funcionalidades:
  - Converte arquivos .zpl e .txt para PDF
  - Processa arquivos ZIP com multiplas etiquetas
  - Suporta configuracao de tamanho, DPI e orientacao
  - Modo watch-folder (monitora pasta automaticamente)
  - Gera log detalhado de cada conversao
  - Salva PDFs individuais ou em lote (multiplas paginas)

Uso:
  python zpl_converter.py --input /caminho/entrada --output /caminho/saida
  python zpl_converter.py --watch /caminho/entrada --output /caminho/saida

Autor: Sistema ZPL para PDF
================================================================================
"""

import os
import sys
import time
import json
import argparse
import zipfile
import logging
import re
import io
import hashlib
from datetime import datetime
from pathlib import Path
from typing import List, Tuple, Optional

# =============================================================================
# CONFIGURACOES PADRAO
# =============================================================================

DEFAULT_CONFIG = {
    "label_width_cm": 11.0,
    "label_height_cm": 15.0,
    "density_dpmm": 8,           # 8 dpmm = 203 dpi
    "output_format": "individual",  # "individual" ou "combined"
    "combined_pdf_name": "etiquetas_lote.pdf",
    "watch_interval_seconds": 5,
    "processed_extension": ".processed",
    "error_extension": ".error",
    "log_level": "INFO",
    "api_timeout": 30,
    "max_retries": 3,
    "retry_delay": 2,
}

# =============================================================================
# CONFIGURACAO DE LOGGING
# =============================================================================

def setup_logging(log_level: str = "INFO", log_file: Optional[str] = None):
    """Configura o sistema de logging com cores e arquivo."""
    level = getattr(logging, log_level.upper(), logging.INFO)

    handlers = [logging.StreamHandler(sys.stdout)]
    if log_file:
        handlers.append(logging.FileHandler(log_file, encoding='utf-8'))

    logging.basicConfig(
        level=level,
        format='%(asctime)s | %(levelname)-8s | %(message)s',
        datefmt='%H:%M:%S',
        handlers=handlers
    )
    return logging.getLogger(__name__)

# =============================================================================
# CLASSE PRINCIPAL DO CONVERSOR
# =============================================================================

class ZPLConverter:
    """Conversor de ZPL para PDF usando a API Labelary."""

    def __init__(self, config: dict):
        self.config = {**DEFAULT_CONFIG, **config}
        self.logger = logging.getLogger(__name__)
        self.session = self._create_session()
        self.converted_count = 0
        self.error_count = 0

    def _create_session(self):
        """Cria uma sessao HTTP com retry e timeout."""
        import urllib.request
        import urllib.error
        return urllib.request

    def validate_zpl(self, code: str) -> Tuple[bool, str]:
        """Valida se o codigo ZPL esta sintaticamente correto."""
        code = code.strip()
        if not code:
            return False, "Codigo ZPL vazio"
        if '^XA' not in code:
            return False, "Codigo deve conter ^XA (inicio)"
        if '^XZ' not in code:
            return False, "Codigo deve conter ^XZ (fim)"
        # Verificar pares XA/XZ
        xa_count = code.count('^XA')
        xz_count = code.count('^XZ')
        if xa_count != xz_count:
            return False, f"Desbalanceado: {xa_count} ^XA e {xz_count} ^XZ"
        return True, "OK"

    def convert_zpl_to_image(self, zpl_code: str) -> bytes:
        """Converte codigo ZPL em imagem PNG usando a API Labelary."""
        import urllib.request
        import urllib.error

        w = self.config["label_width_cm"] / 2.54
        h = self.config["label_height_cm"] / 2.54
        density = self.config["density_dpmm"]

        url = (
            f"http://api.labelary.com/v1/printers/{density}dpmm/"
            f"labels/{w:.4f}x{h:.4f}/0/"
        )

        headers = {
            'Content-Type': 'application/x-www-form-urlencoded',
            'Accept': 'image/png',
        }

        data = zpl_code.encode('utf-8')
        req = urllib.request.Request(url, data=data, headers=headers, method='POST')

        max_retries = self.config["max_retries"]
        retry_delay = self.config["retry_delay"]
        timeout = self.config["api_timeout"]

        last_error = None
        for attempt in range(1, max_retries + 1):
            try:
                with urllib.request.urlopen(req, timeout=timeout) as response:
                    if response.status == 200:
                        return response.read()
                    else:
                        last_error = f"HTTP {response.status}"
            except urllib.error.HTTPError as e:
                last_error = f"HTTP {e.code}: {e.reason}"
                if e.code == 400:
                    # Erro 400 = codigo ZPL invalido, nao retry
                    raise ValueError(f"Codigo ZPL invalido: {last_error}")
            except Exception as e:
                last_error = str(e)

            if attempt < max_retries:
                self.logger.warning(f"  Tentativa {attempt} falhou: {last_error}. Retentando em {retry_delay}s...")
                time.sleep(retry_delay)
            else:
                raise ConnectionError(f"Falha apos {max_retries} tentativas: {last_error}")

        return b""

    def split_multiple_zpl(self, code: str) -> List[str]:
        """Separa multiplos codigos ZPL concatenados."""
        code = code.strip()
        if not code:
            return []

        # Se o codigo contem imagens embutidas (~DGR:), cada etiqueta
        # comeca com ~DGR: e inclui os blocos ^XA...^XZ que a utilizam.
        if '~DGR:' in code:
            # Usa lookahead para dividir no ~DGR: sem consumir o delimitador
            parts = re.split(r'(?=~DGR:)', code)
            result = []
            for part in parts:
                part = part.strip()
                if not part:
                    continue
                result.append(part)
            return result

        # Divide por ^XA...^XZ blocos (etiquetas sem imagem embutida)
        pattern = r'\^XA.*?\^XZ'
        matches = re.findall(pattern, code, re.DOTALL)

        if matches:
            return [m.strip() for m in matches if m.strip()]

        # Se nao encontrou padrao, tenta dividir por quebras de linha
        parts = re.split(r'\n\s*\n', code)
        return [p.strip() for p in parts if p.strip()]

    def extract_from_zip(self, zip_path: str) -> List[Tuple[str, str]]:
        """Extrai arquivos ZPL/TXT de um ZIP."""
        items = []
        try:
            with zipfile.ZipFile(zip_path, 'r') as zf:
                for name in zf.namelist():
                    if name.endswith('/'):
                        continue
                    ext = Path(name).suffix.lower()
                    if ext in ('.zpl', '.txt'):
                        content = zf.read(name).decode('utf-8', errors='replace')
                        items.append((name, content))
        except zipfile.BadZipFile:
            self.logger.error(f"  Arquivo ZIP corrompido: {zip_path}")
        return items

    def process_file(self, file_path: str, output_dir: str) -> List[str]:
        """Processa um unico arquivo e retorna lista de PDFs gerados."""
        path = Path(file_path)
        ext = path.suffix.lower()
        generated_pdfs = []

        self.logger.info(f"Processando: {path.name}")

        # Ler conteudo
        if ext == '.zip':
            items = self.extract_from_zip(str(path))
        elif ext in ('.zpl', '.txt'):
            try:
                content = path.read_text(encoding='utf-8', errors='replace')
                items = [(path.name, content)]
            except Exception as e:
                self.logger.error(f"  Erro ao ler arquivo: {e}")
                return []
        else:
            self.logger.warning(f"  Extensao nao suportada: {ext}")
            return []

        # Processar cada item
        for item_name, zpl_code in items:
            zpl_parts = self.split_multiple_zpl(zpl_code)

            if not zpl_parts:
                self.logger.warning(f"  {item_name}: Nenhum codigo ZPL encontrado")
                continue

            self.logger.info(f"  {item_name}: {len(zpl_parts)} etiqueta(s) encontrada(s)")

            images = []
            for i, part in enumerate(zpl_parts, 1):
                valid, msg = self.validate_zpl(part)
                if not valid:
                    self.logger.warning(f"    Etiqueta {i}: {msg}")
                    self.error_count += 1
                    continue

                try:
                    img_data = self.convert_zpl_to_image(part)
                    images.append(img_data)
                    self.logger.info(f"    Etiqueta {i}: OK")
                except Exception as e:
                    self.logger.error(f"    Etiqueta {i}: ERRO - {e}")
                    self.error_count += 1

            # Gerar PDF(s)
            if images:
                base_name = Path(item_name).stem
                if self.config["output_format"] == "individual":
                    for i, img_data in enumerate(images, 1):
                        if len(images) > 1:
                            pdf_name = f"{base_name}_etiqueta_{i}.pdf"
                        else:
                            pdf_name = f"{base_name}.pdf"
                        pdf_path = os.path.join(output_dir, pdf_name)
                        self._create_pdf_from_image(img_data, pdf_path)
                        generated_pdfs.append(pdf_path)
                        self.logger.info(f"  PDF gerado: {pdf_name}")
                        self.converted_count += 1
                else:
                    # Combined PDF
                    pdf_name = f"{base_name}.pdf"
                    pdf_path = os.path.join(output_dir, pdf_name)
                    self._create_pdf_combined(images, pdf_path)
                    generated_pdfs.append(pdf_path)
                    self.logger.info(f"  PDF combinado gerado: {pdf_name}")
                    self.converted_count += len(images)

        return generated_pdfs

    def _create_pdf_from_image(self, img_data: bytes, output_path: str):
        """Cria um PDF de uma unica imagem."""
        from reportlab.lib.pagesizes import landscape, portrait
        from reportlab.lib.units import cm
        from reportlab.pdfgen import canvas
        from reportlab.lib.utils import ImageReader

        w_cm = self.config["label_width_cm"]
        h_cm = self.config["label_height_cm"]
        w_pt = w_cm * 28.3465
        h_pt = h_cm * 28.3465

        c = canvas.Canvas(output_path, pagesize=(w_pt, h_pt))
        img = ImageReader(io.BytesIO(img_data))
        c.drawImage(img, 0, 0, width=w_pt, height=h_pt, preserveAspectRatio=True)
        c.save()

    def _create_pdf_combined(self, images_data: List[bytes], output_path: str):
        """Cria um PDF com multiplas paginas (uma por imagem)."""
        from reportlab.lib.pagesizes import landscape, portrait
        from reportlab.lib.units import cm
        from reportlab.pdfgen import canvas
        from reportlab.lib.utils import ImageReader

        w_cm = self.config["label_width_cm"]
        h_cm = self.config["label_height_cm"]
        w_pt = w_cm * 28.3465
        h_pt = h_cm * 28.3465

        c = canvas.Canvas(output_path, pagesize=(w_pt, h_pt))
        for i, img_data in enumerate(images_data):
            if i > 0:
                c.showPage()
            img = ImageReader(io.BytesIO(img_data))
            c.drawImage(img, 0, 0, width=w_pt, height=h_pt, preserveAspectRatio=True)
        c.save()

    def process_directory(self, input_dir: str, output_dir: str) -> List[str]:
        """Processa todos os arquivos suportados em um diretorio."""
        input_path = Path(input_dir)
        output_path = Path(output_dir)
        output_path.mkdir(parents=True, exist_ok=True)

        all_pdfs = []
        files = sorted(input_path.glob('*'))

        supported = ['.zpl', '.txt', '.zip']
        files = [f for f in files if f.suffix.lower() in supported]

        if not files:
            self.logger.info(f"Nenhum arquivo .zpl, .txt ou .zip encontrado em: {input_dir}")
            return []

        self.logger.info(f"Encontrados {len(files)} arquivo(s) para processar")
        self.logger.info("-" * 60)

        for file in files:
            pdfs = self.process_file(str(file), str(output_path))
            all_pdfs.extend(pdfs)
            self.logger.info("-" * 60)

        return all_pdfs

    def watch_folder(self, input_dir: str, output_dir: str):
        """Monitora uma pasta e processa novos arquivos automaticamente."""
        input_path = Path(input_dir)
        output_path = Path(output_dir)
        output_path.mkdir(parents=True, exist_ok=True)

        self.logger.info("=" * 60)
        self.logger.info("MODO WATCH-FOLDER ATIVADO")
        self.logger.info(f"Monitorando: {input_dir}")
        self.logger.info(f"Saida: {output_dir}")
        self.logger.info(f"Intervalo: {self.config['watch_interval_seconds']} segundos")
        self.logger.info("Pressione Ctrl+C para parar")
        self.logger.info("=" * 60)

        # Dicionario para rastrear arquivos ja processados
        processed_hashes = set()
        # Carregar historico existente
        history_file = output_path / ".zpl_history.json"
        if history_file.exists():
            try:
                with open(history_file, 'r') as f:
                    data = json.load(f)
                    processed_hashes = set(data.get("processed", []))
                self.logger.info(f"Historico carregado: {len(processed_hashes)} arquivo(s) ja processado(s)")
            except:
                pass

        try:
            while True:
                files = sorted(input_path.glob('*'))
                supported = ['.zpl', '.txt', '.zip']
                files = [f for f in files if f.suffix.lower() in supported]

                new_files = []
                for f in files:
                    file_hash = self._file_hash(str(f))
                    if file_hash not in processed_hashes:
                        new_files.append(f)

                if new_files:
                    self.logger.info(f"\n[{datetime.now().strftime('%H:%M:%S')}] {len(new_files)} novo(s) arquivo(s) detectado(s)")
                    for f in new_files:
                        pdfs = self.process_file(str(f), str(output_path))
                        if pdfs:
                            file_hash = self._file_hash(str(f))
                            processed_hashes.add(file_hash)
                            # Salvar historico
                            with open(history_file, 'w') as fh:
                                json.dump({"processed": list(processed_hashes)}, fh)
                        # Marcar arquivo como processado (renomear)
                        if self.config.get("rename_processed", False):
                            try:
                                processed_name = f.with_suffix(f.suffix + self.config["processed_extension"])
                                f.rename(processed_name)
                                self.logger.info(f"  Arquivo renomeado para: {processed_name.name}")
                            except Exception as e:
                                self.logger.warning(f"  Nao foi possivel renomear arquivo: {e}")

                time.sleep(self.config["watch_interval_seconds"])

        except KeyboardInterrupt:
            self.logger.info("\n" + "=" * 60)
            self.logger.info("Monitoramento encerrado pelo usuario")
            self.logger.info(f"Total convertido: {self.converted_count}")
            self.logger.info(f"Total erros: {self.error_count}")
            self.logger.info("=" * 60)

    def _file_hash(self, file_path: str) -> str:
        """Calcula hash MD5 de um arquivo."""
        h = hashlib.md5()
        try:
            with open(file_path, 'rb') as f:
                for chunk in iter(lambda: f.read(8192), b''):
                    h.update(chunk)
            # Incluir tambem o timestamp de modificacao
            mtime = str(os.path.getmtime(file_path))
            h.update(mtime.encode())
        except:
            pass
        return h.hexdigest()

    def print_summary(self):
        """Imprime resumo da execucao."""
        self.logger.info("=" * 60)
        self.logger.info("RESUMO DA EXECUCAO")
        self.logger.info("=" * 60)
        self.logger.info(f"Etiquetas convertidas com sucesso: {self.converted_count}")
        self.logger.info(f"Etiquetas com erro: {self.error_count}")
        self.logger.info(f"Configuracao: {self.config['label_width_cm']}x{self.config['label_height_cm']} cm | {self.config['density_dpmm']} dpmm")
        self.logger.info("=" * 60)


# =============================================================================
# FUNCOES AUXILIARES
# =============================================================================

def create_sample_zpl(output_dir: str):
    """Cria um arquivo ZPL de exemplo para testes."""
    sample = """^XA
^FO50,50^A0N,50,50^FDETIQUETA DE EXEMPLO^FS
^FO50,120^BY3^BCN,100,Y,N,N^FD123456789012^FS
^FO50,240^A0N,30,30^FDProduto: Teste A123^FS
^FO50,290^A0N,25,25^FDQuantidade: 10 unidades^FS
^XZ
"""
    path = Path(output_dir) / "exemplo.zpl"
    path.write_text(sample, encoding='utf-8')
    print(f"Arquivo de exemplo criado: {path}")
    return str(path)


def load_config(config_file: str) -> dict:
    """Carrega configuracao de arquivo JSON."""
    try:
        with open(config_file, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception as e:
        print(f"Aviso: Nao foi possivel carregar {config_file}: {e}")
        return {}


def save_config(config: dict, config_file: str):
    """Salva configuracao em arquivo JSON."""
    with open(config_file, 'w', encoding='utf-8') as f:
        json.dump(config, f, indent=2, ensure_ascii=False)
    print(f"Configuracao salva em: {config_file}")


# =============================================================================
# MAIN
# =============================================================================

def main():
    parser = argparse.ArgumentParser(
        description='Conversor ZPL para PDF - Automacao em Lote',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Exemplos de uso:
  # Converter todos os arquivos de uma pasta
  python zpl_converter.py -i ./zpl_entrada -o ./pdf_saida

  # Modo watch-folder (monitora pasta automaticamente)
  python zpl_converter.py -i ./zpl_entrada -o ./pdf_saida --watch

  # Com configuracao personalizada de tamanho
  python zpl_converter.py -i ./zpl -o ./pdf --width 10 --height 15 --density 12

  # Criar arquivo de exemplo para testes
  python zpl_converter.py --sample ./zpl_entrada

  # Salvar configuracao padrao
  python zpl_converter.py --save-config config.json
        """
    )

    parser.add_argument('-i', '--input', dest='input_dir',
                        help='Pasta de entrada com arquivos ZPL/TXT/ZIP')
    parser.add_argument('-o', '--output', dest='output_dir',
                        help='Pasta de saida para PDFs gerados')
    parser.add_argument('-w', '--watch', action='store_true',
                        help='Ativa modo watch-folder (monitoramento continuo)')
    parser.add_argument('--width', type=float, default=DEFAULT_CONFIG["label_width_cm"],
                        help=f'Largura da etiqueta em cm (padrao: {DEFAULT_CONFIG["label_width_cm"]})')
    parser.add_argument('--height', type=float, default=DEFAULT_CONFIG["label_height_cm"],
                        help=f'Altura da etiqueta em cm (padrao: {DEFAULT_CONFIG["label_height_cm"]})')
    parser.add_argument('--density', type=int, default=DEFAULT_CONFIG["density_dpmm"],
                        choices=[8, 12, 24],
                        help='Densidade em dpmm: 8=203dpi, 12=300dpi, 24=600dpi (padrao: 8)')
    parser.add_argument('--format', dest='output_format', choices=['individual', 'combined'],
                        default=DEFAULT_CONFIG["output_format"],
                        help='Formato de saida: individual=um PDF por etiqueta, combined=PDF unico (padrao: individual)')
    parser.add_argument('--combined-name', default=DEFAULT_CONFIG["combined_pdf_name"],
                        help='Nome do PDF combinado (padrao: etiquetas_lote.pdf)')
    parser.add_argument('--interval', type=int, default=DEFAULT_CONFIG["watch_interval_seconds"],
                        help=f'Intervalo de verificacao no watch-mode em segundos (padrao: {DEFAULT_CONFIG["watch_interval_seconds"]})')
    parser.add_argument('--rename', action='store_true',
                        help='Renomeia arquivos processados adicionando .processed')
    parser.add_argument('--config', dest='config_file',
                        help='Arquivo JSON de configuracao')
    parser.add_argument('--save-config', dest='save_config',
                        help='Salva configuracao atual em arquivo JSON e sai')
    parser.add_argument('--sample', dest='sample_dir',
                        help='Cria um arquivo ZPL de exemplo na pasta especificada')
    parser.add_argument('--log', dest='log_file',
                        help='Arquivo de log (opcional)')
    parser.add_argument('--log-level', default=DEFAULT_CONFIG["log_level"],
                        choices=['DEBUG', 'INFO', 'WARNING', 'ERROR'],
                        help='Nivel de log (padrao: INFO)')
    parser.add_argument('--preset', choices=['11x15', '10x15', 'fba', 'meli', '10x10'],
                        help='Preset de tamanho: 11x15, 10x15, fba (6x4pol), meli (4x6pol), 10x10')

    args = parser.parse_args()

    # Criar arquivo de exemplo
    if args.sample_dir:
        Path(args.sample_dir).mkdir(parents=True, exist_ok=True)
        create_sample_zpl(args.sample_dir)
        return

    # Salvar configuracao
    if args.save_config:
        config = {**DEFAULT_CONFIG}
        if args.preset:
            presets = {
                '11x15': {'label_width_cm': 11, 'label_height_cm': 15},
                '10x15': {'label_width_cm': 10, 'label_height_cm': 15},
                'fba': {'label_width_cm': 15.24, 'label_height_cm': 10.16},
                'meli': {'label_width_cm': 10.16, 'label_height_cm': 15.24},
                '10x10': {'label_width_cm': 10, 'label_height_cm': 10},
            }
            config.update(presets[args.preset])
        save_config(config, args.save_config)
        return

    # Aplicar preset
    if args.preset:
        presets = {
            '11x15': {'width': 11, 'height': 15},
            '10x15': {'width': 10, 'height': 15},
            'fba': {'width': 15.24, 'height': 10.16},
            'meli': {'width': 10.16, 'height': 15.24},
            '10x10': {'width': 10, 'height': 10},
        }
        p = presets[args.preset]
        args.width = p['width']
        args.height = p['height']

    # Validar argumentos
    if not args.input_dir and not args.sample_dir and not args.save_config:
        print("Erro: Especifique --input ou use --sample/--save-config")
        parser.print_help()
        sys.exit(1)

    if not args.output_dir and not args.sample_dir and not args.save_config:
        print("Erro: Especifique --output")
        parser.print_help()
        sys.exit(1)

    # Carregar configuracao de arquivo
    config = {**DEFAULT_CONFIG}
    if args.config_file:
        config.update(load_config(args.config_file))

    # Sobrescrever com argumentos de linha de comando
    config["label_width_cm"] = args.width
    config["label_height_cm"] = args.height
    config["density_dpmm"] = args.density
    config["output_format"] = args.output_format
    config["combined_pdf_name"] = args.combined_name
    config["watch_interval_seconds"] = args.interval
    config["rename_processed"] = args.rename
    config["log_level"] = args.log_level

    # Setup logging
    logger = setup_logging(args.log_level, args.log_file)

    # Criar conversor
    converter = ZPLConverter(config)

    logger.info("=" * 60)
    logger.info("ZPL PARA PDF - CONVERSOR EM LOTE")
    logger.info("=" * 60)
    logger.info(f"Entrada: {args.input_dir}")
    logger.info(f"Saida: {args.output_dir}")
    logger.info(f"Tamanho: {args.width}x{args.height} cm")
    logger.info(f"Densidade: {args.density} dpmm")
    logger.info(f"Formato: {args.output_format}")
    logger.info("=" * 60)

    # Executar
    if args.watch:
        converter.watch_folder(args.input_dir, args.output_dir)
    else:
        converter.process_directory(args.input_dir, args.output_dir)
        converter.print_summary()

    logger.info("Conversao finalizada!")


if __name__ == '__main__':
    main()
