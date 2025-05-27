-- Trino Test Queries for P2P Workers
-- Run these using the Trino CLI or through the HTTP API

-- 1. Test basic connectivity and worker discovery
SHOW CATALOGS;

-- 2. Check available schemas in TPCH catalog
SHOW SCHEMAS FROM tpch;

-- 3. List tables in TPCH
SHOW TABLES FROM tpch.sf1;

-- 4. Simple query to test data access
SELECT count(*) FROM tpch.sf1.nation;

-- 5. Test distributed query execution across workers
SELECT 
    n.name as nation,
    sum(l.extendedprice * (1 - l.discount)) as revenue
FROM 
    tpch.sf1.customer c,
    tpch.sf1.orders o,
    tpch.sf1.lineitem l,
    tpch.sf1.nation n
WHERE 
    c.custkey = o.custkey
    AND l.orderkey = o.orderkey
    AND c.nationkey = n.nationkey
    AND o.orderdate >= DATE '1994-01-01'
    AND o.orderdate < DATE '1995-01-01'
GROUP BY n.name
ORDER BY revenue DESC
LIMIT 10;

-- 6. Create table in memory catalog
CREATE SCHEMA IF NOT EXISTS memory.test;

CREATE TABLE IF NOT EXISTS memory.test.p2p_test (
    worker_id VARCHAR,
    test_time TIMESTAMP,
    message VARCHAR
);

-- 7. Insert test data
INSERT INTO memory.test.p2p_test VALUES
    ('worker-1', CURRENT_TIMESTAMP, 'Hello from P2P worker 1'),
    ('worker-2', CURRENT_TIMESTAMP, 'Hello from P2P worker 2');

-- 8. Query test data
SELECT * FROM memory.test.p2p_test;

-- 9. Test system information queries
SELECT * FROM system.runtime.nodes;

-- 10. Check query execution stats
SELECT 
    node_id,
    query_id,
    state,
    queued_time,
    analysis_time,
    planning_time
FROM system.runtime.queries
WHERE state = 'FINISHED'
ORDER BY created DESC
LIMIT 5;

-- To execute these queries:
-- 1. Wait for both workers to establish P2P connection
-- 2. Use Trino CLI: trino --server http://<coordinator-url>
-- 3. Or use HTTP API: POST to http://<coordinator-url>/v1/statement