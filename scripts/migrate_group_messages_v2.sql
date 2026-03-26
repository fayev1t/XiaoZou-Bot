-- Migration helper for per-group message tables (v2 schema)
-- This script only creates v2 tables and indexes.
-- Use scripts/migrate_group_messages_v2.py to migrate and format data.

DO $$
DECLARE
    r RECORD;
    old_table TEXT;
    new_table TEXT;
    row_count BIGINT;
    has_message_id BOOLEAN;
BEGIN
    FOR r IN SELECT group_id, table_name FROM groups LOOP
        old_table := r.table_name;
        new_table := format('group_messages_v2_%s', r.group_id);

        IF to_regclass(new_table) IS NULL THEN
            EXECUTE format(
                'CREATE TABLE %I (
                    id SERIAL PRIMARY KEY,
                    user_id BIGINT NOT NULL,
                    onebot_message_id VARCHAR(255),
                    raw_message TEXT,
                    formatted_message TEXT,
                    is_recalled BOOLEAN DEFAULT FALSE,
                    "timestamp" TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP
                )',
                new_table
            );
        END IF;

        EXECUTE format(
            'CREATE INDEX IF NOT EXISTS idx_%I_user_id ON %I(user_id)',
            new_table,
            new_table
        );
        EXECUTE format(
            'CREATE INDEX IF NOT EXISTS idx_%I_onebot_message_id ON %I(onebot_message_id)',
            new_table,
            new_table
        );
        EXECUTE format(
            'CREATE INDEX IF NOT EXISTS idx_%I_is_recalled ON %I(is_recalled)',
            new_table,
            new_table
        );
        EXECUTE format(
            'CREATE INDEX IF NOT EXISTS idx_%I_timestamp ON %I("timestamp")',
            new_table,
            new_table
        );

        EXECUTE format('SELECT COUNT(*) FROM %I', new_table) INTO row_count;
        IF row_count = 0 AND to_regclass(old_table) IS NOT NULL THEN
            SELECT EXISTS (
                SELECT 1
                FROM information_schema.columns
                WHERE table_schema = 'public'
                  AND table_name = old_table
                  AND column_name = 'message_id'
            ) INTO has_message_id;

            IF has_message_id THEN
                EXECUTE format(
                    'INSERT INTO %I (
                        user_id,
                        onebot_message_id,
                        raw_message,
                        formatted_message,
                        is_recalled,
                        "timestamp"
                    )
                    SELECT
                        user_id,
                        message_id::text,
                        message_content,
                        NULL,
                        is_recalled,
                        "timestamp"
                    FROM %I',
                    new_table,
                    old_table
                );
            ELSE
                EXECUTE format(
                    'INSERT INTO %I (
                        user_id,
                        onebot_message_id,
                        raw_message,
                        formatted_message,
                        is_recalled,
                        "timestamp"
                    )
                    SELECT
                        user_id,
                        NULL,
                        message_content,
                        NULL,
                        is_recalled,
                        "timestamp"
                    FROM %I',
                    new_table,
                    old_table
                );
            END IF;
        END IF;
    END LOOP;
END $$;

-- Optional: switch groups.table_name to new tables
-- UPDATE groups SET table_name = format('group_messages_v2_%s', group_id);

-- Optional: drop old tables after verification
-- DO $$
-- DECLARE
--     r RECORD;
-- BEGIN
--     FOR r IN SELECT group_id, table_name FROM groups LOOP
--         EXECUTE format('DROP TABLE IF EXISTS %I', r.table_name);
--     END LOOP;
-- END $$;
