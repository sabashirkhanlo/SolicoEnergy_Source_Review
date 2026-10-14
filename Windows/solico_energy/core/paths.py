from __future__ import annotations
import os
from pathlib import Path

PRODUCT = 'SolicoEnergy'

def user_root() -> Path:
    base = os.environ.get('LOCALAPPDATA') or os.environ.get('APPDATA') or str(Path.home())
    p = Path(base) / PRODUCT
    p.mkdir(parents=True, exist_ok=True)
    return p

def data_dir() -> Path:
    p = user_root() / 'data'; p.mkdir(parents=True, exist_ok=True); return p

def db_path() -> Path: return data_dir() / 'energy.db'
def settings_path() -> Path: return data_dir() / 'settings.json'
def logs_dir() -> Path:
    p = user_root() / 'logs'; p.mkdir(parents=True, exist_ok=True); return p

def updates_dir() -> Path:
    p = user_root() / 'updates'; p.mkdir(parents=True, exist_ok=True); return p

def default_output_dir() -> Path:
    p = Path.home() / 'Documents' / 'Solico Energy Reports'; p.mkdir(parents=True, exist_ok=True); return p
