"""Person-ийн үндсэн түлхүүрийг bigint → UUID.

Дараалсан тоон id нь хаягаар (/person/18/) бусдын бичлэгийг эргэлдүүлэн
таахад хялбар. Одоо байгаа өгөгдлийг алдагдуулахгүйгээр шилжүүлнэ:

1. Хүн бүрд UUID оноож, id → uuid буулгалтын хүснэгт үүсгэнэ.
2. Person руу заасан БҮХ FK-г (pg_constraint-аас динамикаар) түр хасна.
3. FK баганууд болон Person.id-г `ALTER ... TYPE uuid USING _pmap(...)`-ээр
   хөрвүүлнэ — unique/index-үүд автоматаар дахин үүснэ.
4. FK-г Django-гийн хэв маягаар (DEFERRABLE INITIALLY DEFERRED) сэргээнэ.
5. Generic `object_id` (Citation, MediaLink, AuditLog) нь Person ба
   бусад (bigint pk) загвар руу заадаг тул varchar болно; Person-ийн мөрийг
   UUID текстээр сольно. django_admin_log ч мөн адил.

USING-д subquery зөвшөөрөгдөхгүй тул буулгалтыг функцээр дамжуулна.
"""

import uuid

from django.db import migrations, models

FORWARD_SQL = r"""
CREATE TEMP TABLE _person_idmap ON COMMIT DROP AS
    SELECT id, gen_random_uuid() AS uuid FROM genealogy_person;
ALTER TABLE _person_idmap ADD PRIMARY KEY (id);

CREATE FUNCTION pg_temp._pmap(bigint) RETURNS uuid LANGUAGE sql STABLE AS
    $$ SELECT uuid FROM _person_idmap WHERE id = $1 $$;
CREATE FUNCTION pg_temp._person_ct() RETURNS integer LANGUAGE sql STABLE AS
    $$ SELECT id FROM django_content_type
        WHERE app_label = 'genealogy' AND model = 'person' $$;

-- 2. Person руу заасан FK-г бүртгэж, хасна.
CREATE TEMP TABLE _person_fks ON COMMIT DROP AS
    SELECT c.conname, c.conrelid::regclass::text AS tbl, a.attname AS col
      FROM pg_constraint c
      JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = c.conkey[1]
     WHERE c.confrelid = 'genealogy_person'::regclass AND c.contype = 'f';

DO $$
DECLARE r record;
BEGIN
    FOR r IN SELECT * FROM _person_fks LOOP
        EXECUTE format('ALTER TABLE %s DROP CONSTRAINT %I', r.tbl, r.conname);
    END LOOP;
END $$;

-- 3. FK баганууд → uuid.
DO $$
DECLARE r record;
BEGIN
    FOR r IN SELECT DISTINCT tbl, col FROM _person_fks LOOP
        EXECUTE format('ALTER TABLE %s ALTER COLUMN %I TYPE uuid USING pg_temp._pmap(%I)',
                       r.tbl, r.col, r.col);
    END LOOP;
END $$;

-- Person.id → uuid (identity-г эхлээд хасна).
ALTER TABLE genealogy_person ALTER COLUMN id DROP IDENTITY IF EXISTS;
ALTER TABLE genealogy_person ALTER COLUMN id TYPE uuid USING pg_temp._pmap(id);

-- 4. FK-г сэргээнэ.
DO $$
DECLARE r record;
BEGIN
    FOR r IN SELECT * FROM _person_fks LOOP
        EXECUTE format(
            'ALTER TABLE %s ADD CONSTRAINT %I FOREIGN KEY (%I) '
            'REFERENCES genealogy_person (id) DEFERRABLE INITIALLY DEFERRED',
            r.tbl, r.conname, r.col);
    END LOOP;
END $$;

-- 5. Generic object_id → varchar (Person-ийнх нь UUID текст).
-- PositiveBigIntegerField-ийн `object_id >= 0` CHECK-ийг эхлээд хасна.
DO $$
DECLARE r record;
BEGIN
    FOR r IN
        SELECT c.conname, c.conrelid::regclass::text AS tbl
          FROM pg_constraint c
          JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = ANY(c.conkey)
         WHERE c.contype = 'c' AND a.attname = 'object_id'
           AND c.conrelid IN ('genealogy_citation'::regclass, 'genealogy_medialink'::regclass,
                              'genealogy_auditlog'::regclass)
    LOOP
        EXECUTE format('ALTER TABLE %s DROP CONSTRAINT %I', r.tbl, r.conname);
    END LOOP;
END $$;

ALTER TABLE genealogy_citation ALTER COLUMN object_id TYPE varchar(64) USING (
    CASE WHEN content_type_id = pg_temp._person_ct()
         THEN pg_temp._pmap(object_id)::text ELSE object_id::text END);
ALTER TABLE genealogy_medialink ALTER COLUMN object_id TYPE varchar(64) USING (
    CASE WHEN content_type_id = pg_temp._person_ct()
         THEN pg_temp._pmap(object_id)::text ELSE object_id::text END);
ALTER TABLE genealogy_auditlog ALTER COLUMN object_id TYPE varchar(64) USING (
    CASE WHEN object_id IS NULL THEN NULL
         WHEN content_type_id = pg_temp._person_ct()
         THEN COALESCE(pg_temp._pmap(object_id)::text, object_id::text)
         ELSE object_id::text END);

UPDATE django_admin_log SET object_id = pg_temp._pmap(object_id::bigint)::text
 WHERE content_type_id = pg_temp._person_ct() AND object_id ~ '^[0-9]+$'
   AND pg_temp._pmap(object_id::bigint) IS NOT NULL;
"""


class Migration(migrations.Migration):
    # DDL ба өгөгдөл нэг transaction-д — алдаа гарвал бүхэлдээ буцна.
    atomic = True

    dependencies = [
        ("genealogy", "0007_alter_person_visibility"),
        ("accounts", "0003_alter_invite_person"),
        ("admin", "0003_logentry_add_action_flag_choices"),
        ("contenttypes", "0002_remove_content_type_name"),
    ]

    operations = [
        migrations.SeparateDatabaseAndState(
            database_operations=[
                # Буцаах боломжгүй: UUID → тоон id-г сэргээх буулгалт хадгалагдахгүй.
                migrations.RunSQL(FORWARD_SQL),
            ],
            state_operations=[
                migrations.AlterField(
                    model_name="person", name="id",
                    field=models.UUIDField(default=uuid.uuid4, editable=False,
                                           primary_key=True, serialize=False),
                ),
                migrations.AlterField(
                    model_name="citation", name="object_id",
                    field=models.CharField(max_length=64),
                ),
                migrations.AlterField(
                    model_name="medialink", name="object_id",
                    field=models.CharField(max_length=64),
                ),
                migrations.AlterField(
                    model_name="auditlog", name="object_id",
                    field=models.CharField(blank=True, max_length=64, null=True),
                ),
            ],
        ),
    ]
