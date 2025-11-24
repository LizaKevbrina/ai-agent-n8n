-- ===============================================
-- AI Agent Database Schema - FINAL VERSION
-- PostgreSQL + pgvector for Supabase
-- ===============================================

-- Enable extensions
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";
CREATE EXTENSION IF NOT EXISTS "pg_trgm";

-- ===============================================
-- Chat Memory Table
-- ===============================================
CREATE TABLE IF NOT EXISTS chat_memory (
    id SERIAL PRIMARY KEY,
    session_id VARCHAR(100) NOT NULL,
    role VARCHAR(20) NOT NULL CHECK (role IN ('user', 'assistant', 'system')),
    content TEXT NOT NULL,
    created_at TIMESTAMP DEFAULT NOW()
);

-- Indexes for performance
CREATE INDEX IF NOT EXISTS idx_chat_memory_session 
    ON chat_memory(session_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_chat_memory_created 
    ON chat_memory(created_at DESC);

-- ===============================================
-- Prompt Versions Table
-- ===============================================
CREATE TABLE IF NOT EXISTS prompt_versions (
    id SERIAL PRIMARY KEY,
    session_id VARCHAR(100) NOT NULL UNIQUE,
    version VARCHAR(50) NOT NULL,
    updated_at TIMESTAMP DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_prompt_versions_session 
    ON prompt_versions(session_id);

-- ===============================================
-- Logs Table (взаимодействия с AI)
-- ===============================================
CREATE TABLE IF NOT EXISTS logs (
    id SERIAL PRIMARY KEY,
    session_id VARCHAR(100) NOT NULL,
    question TEXT NOT NULL,
    answer TEXT NOT NULL,
    embedding JSONB,
    intent_type VARCHAR(50),
    cached BOOLEAN DEFAULT FALSE,
    response_time_ms INTEGER,
    context_used BOOLEAN DEFAULT FALSE,
    documents_found INTEGER DEFAULT 0,
    timestamp TIMESTAMP DEFAULT NOW(),
    -- NEW fields (2.0)
    input_type VARCHAR(20),  -- 'voice' или 'text'
    stt_duration_ms INTEGER,  -- для голосовых
    chunks_count INTEGER  -- для голосовых
);

CREATE INDEX IF NOT EXISTS idx_logs_session 
    ON logs(session_id, timestamp DESC);

CREATE INDEX IF NOT EXISTS idx_logs_timestamp 
    ON logs(timestamp DESC);

CREATE INDEX IF NOT EXISTS idx_logs_intent 
    ON logs(intent_type);

CREATE INDEX IF NOT EXISTS idx_logs_input_type 
    ON logs(input_type);  -- NEW

-- ===============================================
-- Errors Table (логирование ошибок workflow) - NEW
-- ===============================================
CREATE TABLE IF NOT EXISTS errors (
    id SERIAL PRIMARY KEY,
    error_message TEXT NOT NULL,
    error_code INTEGER,
    node_name VARCHAR(100),
    session_id VARCHAR(100),
    correlation_id VARCHAR(200),
    raw_error TEXT,
    input_type VARCHAR(20),  -- 'voice' или 'text'
    severity VARCHAR(20) DEFAULT 'error',  -- info, warning, error, critical
    timestamp TIMESTAMP DEFAULT NOW()
);

-- Indexes для быстрого поиска
CREATE INDEX IF NOT EXISTS idx_errors_session 
    ON errors(session_id, timestamp DESC);

CREATE INDEX IF NOT EXISTS idx_errors_correlation 
    ON errors(correlation_id);

CREATE INDEX IF NOT EXISTS idx_errors_code 
    ON errors(error_code);

CREATE INDEX IF NOT EXISTS idx_errors_timestamp 
    ON errors(timestamp DESC);

CREATE INDEX IF NOT EXISTS idx_errors_severity 
    ON errors(severity, timestamp DESC);

CREATE INDEX IF NOT EXISTS idx_errors_node 
    ON errors(node_name);

-- ===============================================
-- Helper Functions
-- ===============================================

-- Function: Get logs stats
CREATE OR REPLACE FUNCTION get_logs_stats()
RETURNS TABLE(
    total_logs BIGINT,
    unique_sessions BIGINT,
    avg_response_time_ms NUMERIC,
    real_estate_count BIGINT,
    general_count BIGINT,
    cached_count BIGINT,
    voice_count BIGINT,  -- NEW
    text_count BIGINT,  -- NEW
    avg_stt_duration_ms NUMERIC  -- NEW
) AS $$
BEGIN
    RETURN QUERY
    SELECT
        COUNT(*)::BIGINT as total_logs,
        COUNT(DISTINCT session_id)::BIGINT as unique_sessions,
        AVG(response_time_ms)::NUMERIC as avg_response_time_ms,
        COUNT(*) FILTER (WHERE intent_type = 'real_estate')::BIGINT as real_estate_count,
        COUNT(*) FILTER (WHERE intent_type = 'general')::BIGINT as general_count,
        COUNT(*) FILTER (WHERE cached = TRUE)::BIGINT as cached_count,
        COUNT(*) FILTER (WHERE input_type = 'voice')::BIGINT as voice_count,
        COUNT(*) FILTER (WHERE input_type = 'text')::BIGINT as text_count,
        AVG(stt_duration_ms) FILTER (WHERE stt_duration_ms IS NOT NULL)::NUMERIC as avg_stt_duration_ms
    FROM logs;
END;
$$ LANGUAGE plpgsql;

-- Function: Get error stats (NEW)
CREATE OR REPLACE FUNCTION get_error_stats(days INTEGER DEFAULT 7)
RETURNS TABLE(
    total_errors BIGINT,
    unique_sessions BIGINT,
    errors_by_code JSONB,
    errors_by_node JSONB,
    errors_by_severity JSONB,
    most_affected_sessions JSONB,
    error_rate_by_hour JSONB
) AS $$
BEGIN
    RETURN QUERY
    SELECT
        -- Total errors
        COUNT(*)::BIGINT as total_errors,
        
        -- Unique sessions with errors
        COUNT(DISTINCT session_id)::BIGINT as unique_sessions,
        
        -- Errors by HTTP code
        jsonb_object_agg(
            COALESCE(error_code::TEXT, 'null'), 
            COUNT(*)
        ) as errors_by_code,
        
        -- Errors by node
        jsonb_object_agg(
            COALESCE(node_name, 'Unknown'), 
            COUNT(*)
        ) as errors_by_node,
        
        -- Errors by severity
        jsonb_object_agg(
            severity, 
            COUNT(*)
        ) as errors_by_severity,
        
        -- Most affected sessions
        (
            SELECT jsonb_agg(row_to_json(t))
            FROM (
                SELECT 
                    session_id, 
                    COUNT(*) as error_count,
                    array_agg(DISTINCT node_name) as affected_nodes
                FROM errors
                WHERE timestamp > NOW() - INTERVAL '1 day' * days
                AND session_id IS NOT NULL
                GROUP BY session_id
                ORDER BY error_count DESC
                LIMIT 10
            ) t
        ) as most_affected_sessions,
        
        -- Error rate by hour (last 24h)
        (
            SELECT jsonb_object_agg(
                hour::TEXT, 
                error_count
            )
            FROM (
                SELECT 
                    EXTRACT(HOUR FROM timestamp) as hour,
                    COUNT(*) as error_count
                FROM errors
                WHERE timestamp > NOW() - INTERVAL '24 hours'
                GROUP BY hour
                ORDER BY hour
            ) t
        ) as error_rate_by_hour
        
    FROM errors
    WHERE timestamp > NOW() - INTERVAL '1 day' * days;
END;
$$ LANGUAGE plpgsql;

-- Function: Clean old chat history (>30 days)
CREATE OR REPLACE FUNCTION clean_old_chat_history()
RETURNS INTEGER AS $$
DECLARE
    deleted_count INTEGER;
BEGIN
    DELETE FROM chat_memory
    WHERE created_at < NOW() - INTERVAL '30 days';
    
    GET DIAGNOSTICS deleted_count = ROW_COUNT;
    
    RETURN deleted_count;
END;
$$ LANGUAGE plpgsql;

-- Function: Clean old logs (>90 days)
CREATE OR REPLACE FUNCTION clean_old_logs()
RETURNS INTEGER AS $$
DECLARE
    deleted_count INTEGER;
BEGIN
    DELETE FROM logs
    WHERE timestamp < NOW() - INTERVAL '90 days';
    
    GET DIAGNOSTICS deleted_count = ROW_COUNT;
    
    RETURN deleted_count;
END;
$$ LANGUAGE plpgsql;

-- Function: Clean old errors (>90 days) - NEW
CREATE OR REPLACE FUNCTION clean_old_errors()
RETURNS INTEGER AS $$
DECLARE
    deleted_count INTEGER;
BEGIN
    DELETE FROM errors
    WHERE timestamp < NOW() - INTERVAL '90 days';
    
    GET DIAGNOSTICS deleted_count = ROW_COUNT;
    
    RETURN deleted_count;
END;
$$ LANGUAGE plpgsql;

-- Function: Get session stats
CREATE OR REPLACE FUNCTION get_session_stats(p_session_id VARCHAR)
RETURNS TABLE(
    total_messages BIGINT,
    total_logs BIGINT,
    total_errors BIGINT,
    first_interaction TIMESTAMP,
    last_interaction TIMESTAMP,
    prompt_version VARCHAR,
    avg_response_time_ms NUMERIC,
    error_rate NUMERIC
) AS $$
BEGIN
    RETURN QUERY
    SELECT
        -- Messages count
        (SELECT COUNT(*)::BIGINT FROM chat_memory WHERE session_id = p_session_id) as total_messages,
        
        -- Logs count
        (SELECT COUNT(*)::BIGINT FROM logs WHERE session_id = p_session_id) as total_logs,
        
        -- Errors count
        (SELECT COUNT(*)::BIGINT FROM errors WHERE session_id = p_session_id) as total_errors,
        
        -- First interaction
        (SELECT MIN(timestamp) FROM logs WHERE session_id = p_session_id) as first_interaction,
        
        -- Last interaction
        (SELECT MAX(timestamp) FROM logs WHERE session_id = p_session_id) as last_interaction,
        
        -- Prompt version
        (SELECT version FROM prompt_versions WHERE session_id = p_session_id) as prompt_version,
        
        -- Avg response time
        (SELECT AVG(response_time_ms)::NUMERIC FROM logs WHERE session_id = p_session_id) as avg_response_time_ms,
        
        -- Error rate
        CASE 
            WHEN (SELECT COUNT(*) FROM logs WHERE session_id = p_session_id) > 0 THEN
                (SELECT COUNT(*)::NUMERIC FROM errors WHERE session_id = p_session_id) / 
                (SELECT COUNT(*)::NUMERIC FROM logs WHERE session_id = p_session_id)
            ELSE 0
        END as error_rate;
END;
$$ LANGUAGE plpgsql;

-- Function: Get error timeline (для RCA) - NEW
CREATE OR REPLACE FUNCTION get_error_timeline(p_correlation_id VARCHAR)
RETURNS TABLE(
    timestamp TIMESTAMP,
    event_type VARCHAR,
    node_name VARCHAR,
    error_code INTEGER,
    error_message TEXT,
    session_id VARCHAR
) AS $$
BEGIN
    RETURN QUERY
    -- Errors
    SELECT 
        e.timestamp,
        'error'::VARCHAR as event_type,
        e.node_name,
        e.error_code,
        e.error_message,
        e.session_id
    FROM errors e
    WHERE e.correlation_id = p_correlation_id
    
    UNION ALL
    
    -- Logs (если есть correlation_id в metadata)
    SELECT 
        l.timestamp,
        'interaction'::VARCHAR as event_type,
        'AI Agent'::VARCHAR as node_name,
        NULL::INTEGER as error_code,
        LEFT(l.question, 100)::TEXT as error_message,
        l.session_id
    FROM logs l
    -- WHERE JSON есть correlation_id (требует JSONB поле)
    
    ORDER BY timestamp;
END;
$$ LANGUAGE plpgsql;

-- ===============================================
-- Scheduled Cleanup (optional, requires pg_cron)
-- ===============================================
-- Uncomment if using pg_cron extension:

-- CREATE EXTENSION IF NOT EXISTS pg_cron;
-- 
-- SELECT cron.schedule(
--     'clean-old-history',
--     '0 2 * * *',  -- Every day at 2:00 AM
--     'SELECT clean_old_chat_history();'
-- );
-- 
-- SELECT cron.schedule(
--     'clean-old-logs',
--     '0 3 * * 0',  -- Every Sunday at 3:00 AM
--     'SELECT clean_old_logs();'
-- );
-- 
-- SELECT cron.schedule(
--     'clean-old-errors',
--     '0 4 * * 0',  -- Every Sunday at 4:00 AM
--     'SELECT clean_old_errors();'
-- );

-- ===============================================
-- Sample Data (for testing)
-- ===============================================
-- Uncomment for local testing:

-- INSERT INTO logs (session_id, question, answer, intent_type, input_type, response_time_ms) VALUES
-- ('test_user_1', 'Привет, какие квартиры есть?', 'Здравствуйте! Я помогу подобрать квартиру...', 'real_estate', 'text', 1500),
-- ('test_user_1', 'Две комнаты до 6 млн', 'Отлично! Сейчас проверю базу...', 'real_estate', 'text', 2300),
-- ('test_user_2', 'test voice message', 'Ваше сообщение распознано...', 'general', 'voice', 45000);

-- INSERT INTO errors (error_message, error_code, node_name, session_id, correlation_id, severity) VALUES
-- ('Request timeout', 408, 'LLM Service', 'test_user_1', '2025-01-15T12:34:56-123', 'warning'),
-- ('Service unavailable', 503, 'RAG Service', 'test_user_2', '2025-01-15T12:35:10-456', 'error'),
-- ('Rate limit exceeded', 429, 'Yandex API', 'test_user_1', '2025-01-15T12:36:00-789', 'critical');

-- ===============================================
-- Grants (for production)
-- ===============================================
-- GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO ai_user;
-- GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO ai_user;
-- GRANT EXECUTE ON ALL FUNCTIONS IN SCHEMA public TO ai_user;

-- ===============================================
-- Views для удобства (optional)
-- ===============================================

-- Recent errors view
CREATE OR REPLACE VIEW recent_errors AS
SELECT 
    e.*,
    CASE 
        WHEN e.timestamp > NOW() - INTERVAL '5 minutes' THEN 'just_now'
        WHEN e.timestamp > NOW() - INTERVAL '1 hour' THEN 'recent'
        WHEN e.timestamp > NOW() - INTERVAL '1 day' THEN 'today'
        ELSE 'older'
    END as recency
FROM errors e
WHERE e.timestamp > NOW() - INTERVAL '7 days'
ORDER BY e.timestamp DESC;

-- Session health view
CREATE OR REPLACE VIEW session_health AS
SELECT 
    l.session_id,
    COUNT(DISTINCT l.id) as total_interactions,
    COUNT(DISTINCT e.id) as total_errors,
    CASE 
        WHEN COUNT(DISTINCT l.id) = 0 THEN 0
        ELSE (COUNT(DISTINCT e.id)::NUMERIC / COUNT(DISTINCT l.id)::NUMERIC * 100)
    END as error_rate_pct,
    MAX(l.timestamp) as last_seen,
    MAX(pv.version) as prompt_version
FROM logs l
LEFT JOIN errors e ON l.session_id = e.session_id
LEFT JOIN prompt_versions pv ON l.session_id = pv.session_id
WHERE l.timestamp > NOW() - INTERVAL '30 days'
GROUP BY l.session_id
ORDER BY error_rate_pct DESC, total_interactions DESC;

-- ===============================================
-- Completion Message
-- ===============================================
DO $$
BEGIN
    RAISE NOTICE '================================================';
    RAISE NOTICE 'Database initialization completed successfully!';
    RAISE NOTICE '================================================';
    RAISE NOTICE 'Tables created:';
    RAISE NOTICE '  - chat_memory (conversation history)';
    RAISE NOTICE '  - prompt_versions (prompt version control)';
    RAISE NOTICE '  - logs (AI interactions)';
    RAISE NOTICE '  - errors (workflow errors) [NEW]';
    RAISE NOTICE '';
    RAISE NOTICE 'Functions created:';
    RAISE NOTICE '  - get_logs_stats() (interaction analytics)';
    RAISE NOTICE '  - get_error_stats(days) (error analytics) [NEW]';
    RAISE NOTICE '  - get_session_stats(session_id) (per-session stats)';
    RAISE NOTICE '  - get_error_timeline(correlation_id) (RCA) [NEW]';
    RAISE NOTICE '  - clean_old_* (maintenance)';
    RAISE NOTICE '';
    RAISE NOTICE 'Views created:';
    RAISE NOTICE '  - recent_errors (last 7 days errors)';
    RAISE NOTICE '  - session_health (error rates by session)';
    RAISE NOTICE '================================================';
END $$;
