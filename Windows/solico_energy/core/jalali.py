from datetime import date

# Compact, dependency-free Jalali/Gregorian conversion based on the well-known Jalaali algorithm.

def _div(a,b): return a//b

def gregorian_to_jalali(gy, gm, gd):
    g_d_m = [0,31,59,90,120,151,181,212,243,273,304,334]
    if gy > 1600:
        jy = 979; gy -= 1600
    else:
        jy = 0; gy -= 621
    gy2 = gy + 1 if gm > 2 else gy
    days = 365*gy + _div(gy2+3,4) - _div(gy2+99,100) + _div(gy2+399,400) - 80 + gd + g_d_m[gm-1]
    jy += 33*_div(days,12053); days %= 12053
    jy += 4*_div(days,1461); days %= 1461
    if days > 365:
        jy += _div(days-1,365); days = (days-1)%365
    if days < 186:
        jm = 1 + _div(days,31); jd = 1 + (days%31)
    else:
        jm = 7 + _div(days-186,30); jd = 1 + ((days-186)%30)
    return jy,jm,jd

def jalali_to_gregorian(jy,jm,jd):
    if jy > 979:
        gy = 1600; jy -= 979
    else:
        gy = 621
    days = 365*jy + _div(jy,33)*8 + _div((jy%33)+3,4) + 78 + jd + (jm-1)*31 if jm < 7 else 365*jy + _div(jy,33)*8 + _div((jy%33)+3,4) + 78 + jd + 186 + (jm-7)*30
    gy += 400*_div(days,146097); days %= 146097
    if days > 36524:
        gy += 100*_div(days-1,36524); days = (days-1)%36524
        if days >= 365: days += 1
    gy += 4*_div(days,1461); days %= 1461
    if days > 365:
        gy += _div(days-1,365); days = (days-1)%365
    gd = days + 1
    leap = (gy%4==0 and gy%100!=0) or (gy%400==0)
    sal_a=[0,31,29 if leap else 28,31,30,31,30,31,31,30,31,30,31]
    gm=1
    while gm<=12 and gd>sal_a[gm]: gd-=sal_a[gm]; gm+=1
    return gy,gm,gd

def format_jalali(d: date, sep='/'):
    y,m,dd=gregorian_to_jalali(d.year,d.month,d.day)
    return f'{y:04d}{sep}{m:02d}{sep}{dd:02d}'

def previous_jalali_month_for(d: date):
    y,m,_=gregorian_to_jalali(d.year,d.month,d.day)
    if m==1: return y-1,12
    return y,m-1

def jalali_month_bounds(jy,jm):
    gy,gm,gd=jalali_to_gregorian(jy,jm,1)
    start=date(gy,gm,gd)
    ny,nm=(jy+1,1) if jm==12 else (jy,jm+1)
    gy2,gm2,gd2=jalali_to_gregorian(ny,nm,1)
    from datetime import timedelta
    return start, date(gy2,gm2,gd2)-timedelta(days=1)


def parse_jalali_date(text):
    normalized=str(text).strip().translate(str.maketrans('۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩','01234567890123456789'))
    parts=normalized.replace('-', '/').split('/')
    try:
        if len(parts)!=3: raise ValueError()
        jy,jm,jd=map(int,parts)
        if not (1300<=jy<=1600 and 1<=jm<=12 and 1<=jd<=31): raise ValueError()
        gy,gm,gd=jalali_to_gregorian(jy,jm,jd)
        result=date(gy,gm,gd)
        if gregorian_to_jalali(gy,gm,gd)!=(jy,jm,jd): raise ValueError()
        return result
    except (ValueError,TypeError,OverflowError):
        raise ValueError('تاریخ شمسی معتبر وارد کنید؛ مانند ۱۴۰۵/۰۷/۰۱.') from None
