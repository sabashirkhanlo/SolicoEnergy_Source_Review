from __future__ import annotations
import argparse
from datetime import date
from .core.db import init_db
from .services.importer import import_all
from .reports.daily import generate as daily
from .reports.monthly import generate as monthly

def main():
    init_db(); ap=argparse.ArgumentParser(); sp=ap.add_subparsers(dest='cmd',required=True)
    sp.add_parser('import')
    d=sp.add_parser('daily'); d.add_argument('date')
    m=sp.add_parser('monthly'); m.add_argument('year',type=int);m.add_argument('month',type=int)
    a=ap.parse_args()
    if a.cmd=='import':print(import_all())
    elif a.cmd=='daily':print(daily(date.fromisoformat(a.date),True))
    else:print(monthly(a.year,a.month,True))
if __name__=='__main__':main()
