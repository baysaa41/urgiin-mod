#!/usr/bin/env bash
# Ургийн модны нөөцлөлт — энэ өгөгдөл НӨХӨГДӨХГҮЙ.
#
#   DB: pg_dump -Fc (30 хоногийн эргэлт)
#   Файл: media/ (архивын бичиг, гэрэл зургийн цорын ганц хуулбар байж болно)
#
# Суулгах (өдөр бүр 03:17):
#   crontab -e
#   17 3 * * * /home/deploy/django/urgiin-mod/deploy/backup.sh >> /home/deploy/django/urgiin-mod/logs/backup.log 2>&1
#
# АНХААР: ижил дискэн дээрх нөөцлөлт нь диск эвдрэхэд хамт устана.
# Доорх OFFSITE_DIR (эсвэл rclone/restic) заавал өөр байршилд чиглүүлнэ.
#
# Анхдагч /usr/bin/pg_dump биш, сервертэй ЯГ тохирох client-ийг сонгодог
# тухай доорх "pg_dump/pg_restore-ийг сонгох" хэсгийг уншина уу.

set -euo pipefail

PROJECT_DIR="/home/deploy/django/urgiin-mod"
BACKUP_DIR="${PROJECT_DIR}/backups"
OFFSITE_DIR="${OFFSITE_DIR:-}"           # жишээ: /mnt/backup/urgiin-mod  (гадаад диск/NAS)
KEEP_DAYS=30

# .env-ээс DB тохиргоог унших
set -a
# shellcheck disable=SC1091
source "${PROJECT_DIR}/.env"
set +a

DB_HOST="${DB_HOST:-127.0.0.1}"
DB_PORT="${DB_PORT:-5432}"

# --- pg_dump/pg_restore-ийг сонгох -------------------------------------------
# /usr/bin/pg_dump нь pg_wrapper бөгөөд сервертэй тохирохгүй client сонгож
# болно. 16 сервер рүү 18-ийн client-ээр бичсэн архив нь:
#   * 1.16 форматтай болж, default pg_restore (16) огт уншихгүй —
#     "unsupported version (1.16) in file header";
#   * зөвхөн 17+ -д байдаг тохиргоог архивт оруулж, сэргээлтэд
#     "unrecognized configuration parameter transaction_timeout" алдаа өгдөг.
# Тиймээс серверийн major хувилбартай ЯГ тохирох client-ийг сонгоно.
# (pg_wrapper-ийн `--version` нь хуурамч утга буцаадаг тул түүнд бүү итгэ.)
if [ -z "${PG_BIN:-}" ]; then
    SERVER_MAJOR="$(PGPASSWORD="${DB_PASSWORD}" psql \
        -h "${DB_HOST}" -p "${DB_PORT}" -U "${DB_USER}" -d "${DB_NAME}" \
        -tAc 'show server_version_num' | cut -c1-2)"
    if [ -x "/usr/lib/postgresql/${SERVER_MAJOR}/bin/pg_dump" ]; then
        PG_BIN="/usr/lib/postgresql/${SERVER_MAJOR}/bin"
    else
        echo "АНХААР: /usr/lib/postgresql/${SERVER_MAJOR}/bin олдсонгүй — PATH-ийн pg_dump ашиглана" >&2
        PG_BIN=""
    fi
fi
PG_DUMP="${PG_BIN:+${PG_BIN}/}pg_dump"
PG_RESTORE="${PG_BIN:+${PG_BIN}/}pg_restore"

STAMP="$(date +%Y%m%d-%H%M)"
DUMP_FILE="${BACKUP_DIR}/urgiin_mod-${STAMP}.dump"
mkdir -p "${BACKUP_DIR}"

echo "[$(date -Is)] Нөөцлөлт эхэллээ"

# 1. Мэдээллийн сан
PGPASSWORD="${DB_PASSWORD}" "${PG_DUMP}" \
    -h "${DB_HOST}" -p "${DB_PORT}" -U "${DB_USER}" \
    -Fc -f "${DUMP_FILE}" "${DB_NAME}"
echo "  DB: $(basename "${DUMP_FILE}") ($(du -h "${DUMP_FILE}" | cut -f1))"

# 1б. Архив үнэхээр уншигдаж байгааг шалгах. Уншигдахгүй нөөцлөлт нь
#     нөөцлөлт биш — эвдэрсэн файлыг 30 хоног хадгалахаас энд унах нь дээр.
"${PG_RESTORE}" --list "${DUMP_FILE}" > /dev/null
echo "  шалгалт: архив уншигдаж байна"

# 2. Медиа файлууд (өөрчлөгдсөнийг л)
if [ -d "${PROJECT_DIR}/media" ]; then
    rsync -a --delete "${PROJECT_DIR}/media/" "${BACKUP_DIR}/media/"
    echo "  media: $(du -sh "${BACKUP_DIR}/media" | cut -f1)"
fi

# 3. Хугацаа хэтэрсэн нөөцлөлийг устгах
find "${BACKUP_DIR}" -name 'urgiin_mod-*.dump' -mtime "+${KEEP_DAYS}" -delete
echo "  ${KEEP_DAYS} хоногоос хуучин dump устгагдлаа"

# 4. Гадаад байршилд хуулах (заавал тохируулна!)
if [ -n "${OFFSITE_DIR}" ]; then
    mkdir -p "${OFFSITE_DIR}"
    rsync -a --delete "${BACKUP_DIR}/" "${OFFSITE_DIR}/"
    echo "  offsite: ${OFFSITE_DIR}"
else
    echo "  АНХААР: OFFSITE_DIR тохируулаагүй — нөөцлөлт зөвхөн нэг дискэн дээр байна!" >&2
fi

# 5. Сэргээх эсэхийг турших (сард нэг удаа гараар). Дээрх PG_BIN-тэй ижил
#    хувилбарын pg_restore ашиглана — өөр хувилбараар уншигдахгүй.
#    template1-ийн collation зөрүүнээс (2.39 vs 2.43) болж -T template0 ЗААВАЛ:
#
#      createdb -T template0 -O baysa urgiin_mod_restore
#      /usr/lib/postgresql/<major>/bin/pg_restore \
#          -d urgiin_mod_restore backups/urgiin_mod-YYYYMMDD-HHMM.dump
#      # тоог харьцуулсны дараа:
#      dropdb urgiin_mod_restore
echo "[$(date -Is)] Дууслаа"
