CREATE SCHEMA IF NOT EXISTS "aws";
CREATE SCHEMA IF NOT EXISTS "workspace";

-- Sequence lives off `public` so it's invisible to BaseService.sync_sequences,
-- which otherwise rewinds it under concurrent /api/reset and breaks
-- _changelog_pkey. No OWNED BY: cross-schema link isn't allowed and we don't
-- drop _changelog anyway.
CREATE SCHEMA IF NOT EXISTS agent_env_internal;
CREATE SEQUENCE IF NOT EXISTS agent_env_internal._changelog_id_seq;
CREATE TABLE IF NOT EXISTS public._changelog (
    id BIGINT PRIMARY KEY DEFAULT nextval('agent_env_internal._changelog_id_seq'),
    "timestamp" TIMESTAMPTZ NOT NULL DEFAULT now(),
    schema_name TEXT NOT NULL,
    table_name TEXT NOT NULL,
    operation TEXT NOT NULL CHECK (operation IN ('INSERT','UPDATE','DELETE')),
    row_id TEXT,
    summary TEXT NOT NULL,
    changed_fields JSONB
);

CREATE OR REPLACE FUNCTION public._changelog_trigger_fn()
RETURNS TRIGGER AS $$
DECLARE
    _row_id TEXT;
    _summary TEXT;
    _changed JSONB;
    _old_json JSONB;
    _new_json JSONB;
    _key TEXT;
    _old_val TEXT;
    _new_val TEXT;
    _changes TEXT[];
    _pk_cols TEXT[];
    _rec JSONB;
BEGIN
    -- Serialize concurrent writers to the shared public._changelog table.
    -- Re-entrant within a transaction (a multi-thousand-row bulk load takes
    -- it once on the first trigger fire; subsequent fires in the same xact
    -- are no-ops). Across transactions, the second waits until the first
    -- commits — eliminates deadlocks and serial-PK collisions during
    -- concurrent /load-universe resets without serializing user-edit
    -- traffic beyond the changelog write itself.
    PERFORM pg_advisory_xact_lock(hashtext('public._changelog'));

    -- Capture the changed row's identity from the table's ACTUAL primary key
    -- (not a hardcoded 'id' column — universe tables use varied PKs, e.g.
    -- record_id). Single-column PKs are stored as the plain value; composite
    -- PKs as an unambiguous JSON object (so values containing a delimiter can't
    -- collide); PK-less tables fall back to an 'id' column if present, else NULL.
    IF TG_OP = 'DELETE' THEN _rec := row_to_json(OLD)::jsonb;
    ELSE _rec := row_to_json(NEW)::jsonb; END IF;

    SELECT array_agg(a.attname ORDER BY k.ord)
    INTO _pk_cols
    FROM pg_index i
    JOIN LATERAL unnest(i.indkey) WITH ORDINALITY AS k(attnum, ord) ON true
    JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = k.attnum
    WHERE i.indrelid = TG_RELID AND i.indisprimary;

    IF _pk_cols IS NULL THEN
        _row_id := _rec ->> 'id';
    ELSIF array_length(_pk_cols, 1) = 1 THEN
        _row_id := _rec ->> _pk_cols[1];
    ELSE
        SELECT jsonb_object_agg(col, _rec -> col)::text
        INTO _row_id
        FROM unnest(_pk_cols) AS col;
    END IF;

    IF TG_OP = 'INSERT' THEN
        _changed := row_to_json(NEW)::jsonb;
        _summary := 'Inserted: ' || left(_changed::text, 200);

    ELSIF TG_OP = 'DELETE' THEN
        _changed := row_to_json(OLD)::jsonb;
        _summary := 'Deleted: ' || left(_changed::text, 200);

    ELSIF TG_OP = 'UPDATE' THEN
        _old_json := row_to_json(OLD)::jsonb;
        _new_json := row_to_json(NEW)::jsonb;
        _changed := '{}'::jsonb;
        _changes := ARRAY[]::TEXT[];

        FOR _key IN SELECT jsonb_object_keys(_new_json)
        LOOP
            IF _old_json -> _key IS DISTINCT FROM _new_json -> _key THEN
                _changed := _changed || jsonb_build_object(
                    _key, jsonb_build_object('old', _old_json -> _key, 'new', _new_json -> _key)
                );
                _old_val := left(_old_json ->> _key, 50);
                _new_val := left(_new_json ->> _key, 50);
                _changes := array_append(
                    _changes,
                    _key || ': ' || COALESCE(quote_literal(_old_val), 'NULL')
                         || ' -> ' || COALESCE(quote_literal(_new_val), 'NULL')
                );
            END IF;
        END LOOP;

        _summary := array_to_string(_changes, ', ');
    END IF;

    INSERT INTO public._changelog (schema_name, table_name, operation, row_id, summary, changed_fields)
    VALUES (TG_TABLE_SCHEMA, TG_TABLE_NAME, TG_OP, _row_id, _summary, _changed);

    RETURN COALESCE(NEW, OLD);
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION public._install_changelog_triggers(_schema_name TEXT)
RETURNS void AS $$
DECLARE
    _tbl RECORD;
BEGIN
    DELETE FROM public._changelog WHERE schema_name = _schema_name;

    FOR _tbl IN
        SELECT schemaname, tablename
        FROM pg_tables
        WHERE schemaname = _schema_name
          AND tablename != '_changelog'
    LOOP
        EXECUTE format(
            'DROP TRIGGER IF EXISTS _changelog_trigger ON %I.%I',
            _tbl.schemaname, _tbl.tablename
        );
        EXECUTE format(
            'CREATE TRIGGER _changelog_trigger '
            'AFTER INSERT OR UPDATE OR DELETE ON %I.%I '
            'FOR EACH ROW EXECUTE FUNCTION public._changelog_trigger_fn()',
            _tbl.schemaname, _tbl.tablename
        );
    END LOOP;
END;
$$ LANGUAGE plpgsql;
