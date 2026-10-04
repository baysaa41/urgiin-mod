# Production байрлуулалт — `family.integral.mn`

Ubuntu 26 сервер дээр «Ургийн мод»-ыг ажиллуулах бүрэн удирдамж.
Локал хөгжүүлэлт нь Python 3.14.4, PostgreSQL 16 дээр ажиллаж байсан.

```
Хэрэглэгч ──HTTPS──▶ nginx ──unix socket──▶ gunicorn (3 worker) ──▶ Django
                        │                                              │
                        └── /static/  (Whitenoise-ийн цуглуулсан файл)  └── PostgreSQL
```

---

## 1. GitHub-д байршуулах

Репо нь одоогоор git БИШ (`git status` → «not a repository»). Эхлээд түүхийг
үүсгэж, GitHub руу илгээнэ — сервер тэндээс `git clone` хийнэ.

### 1.1 Юу БАЙРШУУЛАХГҮЙ вэ

`.gitignore`-д аль хэдийн хаалттай ч нэг удаа нүдээр шалгана уу:

| Файл / хавтас | Яагаад |
|---|---|
| `.env` | `SECRET_KEY`, `DB_PASSWORD`, SMTP нууц үг |
| `backups/*.dump` | **бүхэл удмын сан** — нэг файлд бүх хүний мэдээлэл |
| `media/` | гэрэл зураг, архивын бичиг (хувийн) |
| `staticfiles/`, `logs/`, `.venv/` | үүсгэгддэг, хэмжээ ихтэй |

> `backups/` дотор 30 хоногийн эргэлттэй dump байдаг. GitHub руу санамсаргүй
> орвол буцааж устгах боломжгүй (түүхээс үлдэнэ) — private repo байсан ч
> бүх удмын сан нэг таблет дээр гарна.

### 1.2 Түүх үүсгэх

```bash
cd /home/deploy/django/urgiin-mod

git init -b main
git add .
git status                 # ← .env, backups/, media/ ОРООГҮЙ эсэхийг ШАЛГА
git commit -m "Ургийн мод: анхны хувилбар"
```

`git status`-д `.env` эсвэл `.dump` харагдвал `git rm --cached <файл>` хийгээд
`.gitignore`-д нэмнэ үү.

### 1.3 GitHub руу илгээх

```bash
# GitHub дээр хоосон repo үүсгэсний дараа (README/.gitignore нэмэхгүй):
git remote add origin git@github.com:<ORG>/<REPO>.git
git push -u origin main
```

SSH түлхүүр байхгүй бол:
```bash
ssh-keygen -t ed25519 -C "deploy@family.integral.mn"
cat ~/.ssh/id_ed25519.pub        # → GitHub → Settings → SSH keys
```

**Private repo** сонгохыг зөвлөнө — код нь гэр бүлийн бүтцийн логикийг агуулна.

---

## 2. Серверийн урьдчилсан нөхцөл

- Ubuntu 26 (LTS), `sudo` эрхтэй хэрэглэгч
- `family.integral.mn` → серверийн IP руу заасан **A record** (TLS авахаас өмнө
  DNS ажиллаж байх ёстой)
- 80, 443 порт нээлттэй

---

## 3. Системийн багцууд

```bash
sudo apt update
sudo apt install -y python3 python3-venv python3-dev \
                    postgresql postgresql-contrib libpq-dev \
                    nginx git certbot python3-certbot-nginx build-essential

python3 -V          # 3.14.x байх ёстой
psql --version      # 16+ (13+ хангалттай — доорх тайлбарыг харна)
```

> **PostgreSQL 13+** шаардана: хайлтын `pg_trgm` индекс нь migration
> `genealogy/0001` дотор `TrigramExtension()`-ээр үүсдэг бөгөөд `pg_trgm` нь
> 13-аас хойш «trusted» extension тул **сангийн эзэн** superuser-гүйгээр
> үүсгэж чадна. Тиймээс доор `urgiin` хэрэглэгчийг сангийн ЭЗЭН болгоно.

---

## 4. Хэрэглэгч ба хавтас

```bash
sudo adduser --disabled-password --gecos "" deploy

# nginx (www-data) нь gunicorn-ы сокет руу холбогдохын тулд deploy бүлэгт
# байх ёстой — эс бөгөөс 502 Bad Gateway (сокет 0660, эзэн нь deploy).
sudo usermod -aG deploy www-data
```

---

## 5. PostgreSQL

```bash
sudo -u postgres psql
```

```sql
CREATE USER urgiin WITH PASSWORD 'САНАМСАРЛАГАТАЙ_НУУЦ_ҮГ';
CREATE DATABASE urgiin_mod OWNER urgiin ENCODING 'UTF8' LC_COLLATE 'C' LC_CTYPE 'C' TEMPLATE template0;
ALTER ROLE urgiin SET client_encoding TO 'utf8';
ALTER ROLE urgiin SET default_transaction_isolation TO 'read committed';
ALTER ROLE urgiin SET timezone TO 'Asia/Ulaanbaatar';
\q
```

`OWNER urgiin` нь чухал — `pg_trgm`-ийг migration өөрөө үүсгэнэ.

Гаднаас холбогдох шаардлагагүй: `pg_hba.conf`-ыг хөндөхгүй (локал socket хангалттай).

---

## 6. Код ба venv

```bash
sudo -u deploy -H bash
mkdir -p /home/deploy/django
git clone git@github.com:<ORG>/<REPO>.git /home/deploy/django/urgiin-mod
cd /home/deploy/django/urgiin-mod

python3 -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -r requirements.txt
```

---

## 7. `.env` (production)

```bash
cp .env.example .env
python3 -c "import secrets; print(secrets.token_urlsafe(64))"   # SECRET_KEY
nano .env
```

`family.integral.mn`-д тохирсон утгууд:

```ini
DJANGO_DEBUG=0
SECRET_KEY=<дээрх санамсаргүй мөр>
DJANGO_ALLOWED_HOSTS=family.integral.mn,127.0.0.1,localhost
DJANGO_CSRF_TRUSTED_ORIGINS=https://family.integral.mn

DB_NAME=urgiin_mod
DB_USER=urgiin
DB_PASSWORD=<5-р алхамын нууц үг>
DB_HOST=127.0.0.1
DB_PORT=5432

LIVING_YEARS=110
INVITE_EXPIRY_DAYS=7
SITE_URL=https://family.integral.mn

# Урилгын имэйл. SMTP-гүй бол console үлдээвэл имэйл зөвхөн лог руу бичигдэнэ
# (холбоосыг админ «Урилгууд» хуудсаас хуулж өгч болно).
EMAIL_BACKEND=django.core.mail.backends.smtp.EmailBackend
EMAIL_HOST=<smtp сервер>
EMAIL_PORT=587
EMAIL_HOST_USER=<хэрэглэгч>
EMAIL_HOST_PASSWORD=<нууц үг>
EMAIL_USE_TLS=1
DEFAULT_FROM_EMAIL=urgiin-mod@integral.mn
```

```bash
chmod 600 .env
```

> `SECRET_KEY` хоосон үлдвэл `DJANGO_DEBUG=0` үед Django **асахгүй**
> (`settings.py` доторх guard) — энэ нь санаатай.

---

## 8. Миграц ба static

```bash
.venv/bin/python manage.py migrate
.venv/bin/python manage.py collectstatic --noinput
```

Хүлээгдэж буй миграцууд (бүгд локал дээр ажилласан):

| Апп | Миграц |
|---|---|
| genealogy | `0007_alter_person_visibility` («Хаалттай» түвшин) |
| accounts | `0003_alter_invite_person` (урилга заавал хүнтэй) |

`collectstatic` нь `staticfiles/` руу хуулж, Whitenoise (manifest) үйлчилнэ.

---

## 9. Админ хэрэглэгч

Хэрэглэгчийн загвар нь **имэйлээр** нэвчирдэг (`AUTH_USER_MODEL = accounts.User`):

```bash
.venv/bin/python manage.py createsuperuser
# Email: таны@имэйл
# Role: admin  (эсвэл Django admin-аас сүүлд нь тавина)
```

---

## 10. systemd (gunicorn)

```bash
sudo cp deploy/urgiin-mod.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now urgiin-mod
systemctl status urgiin-mod
```

Unit нь `RuntimeDirectory=urgiin-mod`-оор `/run/urgiin-mod/` хавтсыг үүсгэж,
gunicorn тэнд `gunicorn.sock` сокетыг байрлуулна. **Энэ мөргүй бол**
`/run` нь root-ынх тул `Permission denied` гараад сервис асахгүй.

`--umask 0007` нь сокетыг `0660` болгоно: nginx сокет руу **холбогдохын тулд
бичих эрх** шаардана. Тиймээс §4-ийн `usermod -aG deploy www-data` ЗААВАЛ,
дараа нь `sudo systemctl restart nginx`.

Лог:
```bash
journalctl -u urgiin-mod -f
tail -f logs/gunicorn-error.log
```

---

## 11. nginx + TLS

`/etc/nginx/sites-available/urgiin-mod`:

```nginx
server {
    listen 80;
    server_name family.integral.mn;

    client_max_body_size 25M;          # гэрэл зураг/архив оруулахад

    location /static/ {
        alias /home/deploy/django/urgiin-mod/staticfiles/;
        expires 30d;
        access_log off;
    }

    # /media/ -ыг nginx ШУУД ҮЙЛЧИЛЭХГҮЙ. Медиа нь эрх шалгадаг
    # `genealogy.views.serve_media` view-ээр дамжина — шууд alias хийвэл
    # «Хувийн»/«Хаалттай» хүний зураг хамгаалалтыг тойрч харагдана.
    location / {
        proxy_pass http://unix:/run/urgiin-mod/gunicorn.sock;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_read_timeout 120s;
    }
}
```

```bash
sudo ln -s /etc/nginx/sites-available/urgiin-mod /etc/nginx/sites-enabled/
sudo nginx -t && sudo systemctl reload nginx
sudo certbot --nginx -d family.integral.mn      # TLS + http→https redirect
```

Certbot нь `http → https` redirect-ыг өөрөө нэмнэ — тиймээс `settings.py` дээр
`SECURE_SSL_REDIRECT` анхдагчаараа (унтарсан) үлдэнэ.

Firewall:
```bash
sudo ufw allow OpenSSH && sudo ufw allow 'Nginx Full' && sudo ufw enable
```

---

## 12. Нөөцлөлт

```bash
crontab -e
```
```cron
17 3 * * * /home/deploy/django/urgiin-mod/deploy/backup.sh >> /home/deploy/django/urgiin-mod/logs/backup.log 2>&1
```

`OFFSITE_DIR`-ыг **өөр диск/сүлжээний байршилд** заавал заана — ижил дискэн
дээрх нөөцлөлт нь диск эвдрэхэд хамт устана.

```bash
# .env-д нэмэх (backup.sh уншина):
OFFSITE_DIR=/mnt/backup/urgiin-mod
```

---

## 13. Шалгах жагсаалт

```bash
curl -I https://family.integral.mn/accounts/login/     # 200, HSTS толгойтой
curl -I http://family.integral.mn/                     # 301 → https
```

- [ ] Нэвтрэх хуудас нээгдэж, имэйлээр нэвтэрч чадаж байна
- [ ] Мод ачаалагдаж, зангилаа дээр дарахад төв солигдож байна
- [ ] `https://family.integral.mn/static/css/app.css` → 200
- [ ] Медиа файл (`/media/<pk>/serve/`) нэвтрээгүй хүнд 404
- [ ] `journalctl -u urgiin-mod` дотор алдаа байхгүй
- [ ] `python manage.py check --deploy` — анхааруулга уншиж үзнэ

---

## 14. Шинэчлэлт (код шинэчлэх)

```bash
cd /home/deploy/django/urgiin-mod
git pull
.venv/bin/pip install -r requirements.txt
.venv/bin/python manage.py migrate
.venv/bin/python manage.py collectstatic --noinput
sudo systemctl restart urgiin-mod
```

**Миграцын өмнө нөөц авна** (`deploy/backup.sh`) — ялангуяа өгөгдөл
хөрвүүлдэг migration байвал.

---

## 15. Анхаарах зүйлс

- **Медиа нь хэзээ ч nginx-ээр шууд үйлчлэгдэхгүй** — эрхийн шалгалт
  Django view дотор байна (§11-ийг харна уу).
- **Хувийн өгөгдөл**: «Хаалттай» хүн зөвхөн админд, «Хувийн» хүний нас/хүйс
  бусдад хаалттай. Эдгээр дүрэм нь query түвшинд (`Person.objects.visible_to`)
  шийдэгддэг тул кэш давхарга нэмбэл түүнийг эвдэхгүй байхыг анхаарна.
- **Django admin** (`/admin/`) нь нэмэлт зам — зөвхөн `is_staff` хэрэглэгч
  нэвтэрнэ. Хэрэглэгчийн `role` талбарыг admin-аас өөрчлөхөд л
  `is_staff`/`is_superuser` хэрэгтэй.
- **SMTP тохируулаагүй** бол урилгын имэйл зөвхөн лог руу бичигдэнэ; холбоосыг
  админ «Ургийн мод → Урилгууд» хуудсаас хуулж өгч болно.

---

## 16. Өгөгдлийг production руу шилжүүлэх ба сэргээх

### 16.1 Юу шилжих вэ

| | Хэрхэн |
|---|---|
| **DB** (хүн, гэр бүл, хэрэглэгч, аудит) | `pg_dump` архив → `pg_restore` |
| `media/` (гэрэл зураг, архив) | тусдаа `rsync` — **dump-д ОРОХГҮЙ** |
| `.env` (нууц) | гараар бичнэ, хэзээ ч dump/репод оруулахгүй |

> Бэлдсэн dump: `backups/PROD-SEED-20261004-2003.dump` (136K).
> Агуулга: Person **1049**, Family **519**, User **1**, миграц **28** мөр.

### 16.2 Дараалал — restore ЭХЭЛЖ, дараа нь migrate

```
1. сан үүсгэх (§5)
2. pg_restore        ← бүтэц + өгөгдөл + django_migrations
3. manage.py migrate ← зөвхөн dump-аас хойшхи дутуу миграцыг хийнэ
4. manage.py collectstatic
```

Dump нь `django_migrations` хүснэгтийг хамт агуулдаг тул `migrate` нь аль
хэрэглэгдсэнийг мэдэж, **зөвхөн дутууг** хийнэ. Хэрэв эсрэгээр (эхлээд
`migrate`, дараа нь `pg_restore --clean`) хийвэл сэргээлт нь дөнгөж
үүсгэсэн бүтцийг устгана.

### 16.3 Шинэ сан руу (эхний байрлуулалт)

Эх сервер дээр:
```bash
cd /home/deploy/django/urgiin-mod
bash deploy/backup.sh
scp backups/urgiin_mod-*.dump deploy@family.integral.mn:/home/deploy/
```

Production сервер дээр:
```bash
cd /home/deploy/django/urgiin-mod

# Сэргээлтийг `urgiin` хэрэглэгчээр ХИЙНЭ. `--no-owner` нь dump доторх
# «эзэн нь baysa» гэсэн зааврыг алгасаж, объектуудыг сэргээж буй
# хэрэглэгчид (urgiin) томилно.
PGPASSWORD='<нууц үг>' pg_restore --no-owner -h 127.0.0.1 -U urgiin \
           -d urgiin_mod /home/deploy/urgiin_mod-*.dump

.venv/bin/python manage.py migrate
.venv/bin/python manage.py collectstatic --noinput
```

### 16.4 Одоо байгаа санг сэргээх (сэргээлт)

⚠️ `--clean` нь одоо байгаа object бүрийг **УСТГААД** дахин үүсгэнэ.
Зөвхөн сэргээх гэж байгаа үедээ л ажиллуулна.

```bash
sudo systemctl stop urgiin-mod              # бичих зогсооно
cd /home/deploy/django/urgiin-mod
bash deploy/backup.sh                        # одоогийн байдлын нөөц (буцаах цэг)

PGPASSWORD='<нууц үг>' pg_restore --clean --if-exists --no-owner \
           -h 127.0.0.1 -U urgiin -d urgiin_mod /home/deploy/urgiin_mod-*.dump

.venv/bin/python manage.py migrate
sudo systemctl start urgiin-mod
```

`--if-exists` нь «устгах object байхгүй» гэсэн алдааг дарна.

### 16.5 Хувилбарын шаардлага — ЧУХАЛ

| | |
|---|---|
| Dump архив | **1.15** (PostgreSQL 16-ийн `pg_dump`) |
| Унших `pg_restore` | **16 ба түүнээс дээш** |
| Хуучин client (15 ба доош) | `unsupported version (1.15) in file header` |

- Production сервер нь **PostgreSQL 16+** байх ёстой. Эс бөгөөс PG16-ийн SQL
  ажиллахгүй байж болно (жишээ нь өмнө нь `transaction_timeout` тохиргооны
  алдаа гарч байсан — PG17+ л мэддэг параметр).
- Сервер дээрх `pg_restore`-ыг өөрийнх нь хувилбартай хамт ашиглана:
  `/usr/lib/postgresql/<major>/bin/pg_restore`.
- `pg_trgm` extension нь dump-д орсон (`EXTENSION - pg_trgm`). Сэргээх role нь
  **сангийн эзэн** байх ёстой (§5) — эс бөгөөс үүсгэж чадахгүй.
- `--role=urgiin` гэж бичиж БОЛНО, гэхдээ зөвхөн холбогдож буй хэрэглэгч
  `urgiin`-ы гишүүн эсвэл superuser байвал. Хамгийн энгийн нь дээрх шиг
  **`urgiin`-ээр холбогдож, `--no-owner`** хэрэглэх.

### 16.6 Шалгах

```bash
.venv/bin/python manage.py shell -c "
from genealogy.models import Person, Family
from django.contrib.auth import get_user_model
print('Person:', Person.objects.count())
print('Family:', Family.objects.count())
print('User  :', get_user_model().objects.count())
"
```

Хүлээгдэж буй: **Person 1049 · Family 519 · User 1**

```bash
.venv/bin/python manage.py showmigrations genealogy accounts | grep -c '\[X\]'   # 10
```

Дараа нь вэбээр: нэвтэрч, мод нээгдэж, хайлт (`pg_trgm`) ажиллаж байгаа эсэх.

### 16.7 Анхаарах

- Dump нь `backups/`-д байрлана ба `.gitignore`-д хаалттай — GitHub руу ОРОХГҮЙ.
  Гэхдээ шилжүүлэхдээ зөвхөн шифрлэгдсэн сувгаар (`scp`/`ssh`) илгээнэ: файлд
  бүхэл удмын сан байна.
- `media/` нь dump-д орохгүй. Одоогийн сан дээр `Media` мөр **0** (media/ дотор
  зөвхөн тестийн үүсгэсэн `test_*.txt` файлууд байна) тул шилжүүлэх зүйлгүй.
  Хожим жинхэнэ гэрэл зураг нэмэгдвэл:
  `rsync -av media/ deploy@family.integral.mn:/home/deploy/django/urgiin-mod/media/`
- Сэргээлтийн дараа `sudo systemctl restart nginx` шаардлагагүй.
