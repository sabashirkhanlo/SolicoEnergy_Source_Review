import logging
from .paths import logs_dir

def setup_logging():
    path = logs_dir() / 'solico_energy.log'
    logging.basicConfig(level=logging.INFO, format='%(asctime)s | %(levelname)s | %(name)s | %(message)s', handlers=[logging.FileHandler(path, encoding='utf-8'), logging.StreamHandler()])
    return path
