-- Remove message_id column from group_messages_v2 tables

DO $$
DECLARE
    r RECORD;
    tbl TEXT;
BEGIN
    IF to_regclass('group_messages_v2_template') IS NOT NULL THEN
        IF EXISTS (
            SELECT 1
            FROM information_schema.columns
            WHERE table_schema = 'public'
              AND table_name = 'group_messages_v2_template'
              AND column_name = 'message_id'
        ) THEN
            ALTER TABLE group_messages_v2_template DROP COLUMN message_id;
        END IF;
        DROP INDEX IF EXISTS idx_group_messages_v2_template_message_id;
    END IF;

    IF to_regclass('group_messages_template') IS NOT NULL THEN
        IF EXISTS (
            SELECT 1
            FROM information_schema.columns
            WHERE table_schema = 'public'
              AND table_name = 'group_messages_template'
              AND column_name = 'message_id'
        ) THEN
            ALTER TABLE group_messages_template DROP COLUMN message_id;
        END IF;
        DROP INDEX IF EXISTS idx_group_messages_template_message_id;
    END IF;

    FOR r IN SELECT group_id, table_name FROM groups LOOP
        tbl := r.table_name;
        IF to_regclass(tbl) IS NOT NULL THEN
            IF EXISTS (
                SELECT 1
                FROM information_schema.columns
                WHERE table_schema = 'public'
                  AND table_name = tbl
                  AND column_name = 'message_id'
            ) THEN
                EXECUTE format('ALTER TABLE %I DROP COLUMN message_id', tbl);
            END IF;
            EXECUTE format('DROP INDEX IF EXISTS idx_%I_message_id', tbl);
        END IF;
    END LOOP;
END $$;
