# SolicoEnergy – Source Review Package

این بسته فقط برای **بررسی کد (Code Review)** تهیه شده است و نسخه عملیاتی/قابل Deploy نیست.

## محتویات
- `Windows/` — سورس بخش Windows نسخه 1.0.27 که برای Review پاک‌سازی شده است.
- `Android/` — سورس اصلی Android بدون کلید امضا و تنظیمات تولید.
- `iPhone-PWA/` — سورس PWA آیفون بدون تنظیمات و Credentialهای تولید.
- `Firebase-Reference/` — فقط فایل‌های مرجع معماری/Rules پاک‌سازی‌شده.
- `Docs/` — توضیحات امنیتی و لیست Hash فایل‌ها.

## مواردی که عمداً حذف یا خنثی شده‌اند
- Android signing keystore و Base64 آن
- Firebase/Google production configuration
- GitHub Actions deployment workflows
- Registry secrets و topicهای تولید
- VAPID private key و production Web Push values
- نام کاربری/Password hashهای عملیاتی
- Production endpoints که برای اتصال به سیستم واقعی لازم‌اند
- فایل‌های Backup/قدیمی و `__pycache__`

## نکته مهم
دریافت‌کننده می‌تواند این کپی را روی سیستم خودش ویرایش کند، چون هیچ فایل سورسی که در اختیار شخص دیگری قرار بگیرد واقعاً «غیرقابل ویرایش» نیست.
اما این بسته Credential، کلید امضا یا مسیر Deploy سیستم واقعی را ندارد و برای تغییر/آپدیت مستقیم نسخه عملیاتی SolicoEnergy آماده نیست.

برای اطمینان از اینکه فایل‌ها بعداً تغییر نکرده‌اند، Hash هر فایل در `Docs/MANIFEST_SHA256.txt` ثبت شده است.
