-- Multi-schema e-commerce/logistics test database
-- Exercises: cross-schema FKs, FK cycle, JSONB, composite PK, passthrough, disconnected

-- ============================================================
-- SCHEMAS
-- ============================================================
CREATE SCHEMA inventory;
CREATE SCHEMA sales;

-- ============================================================
-- INVENTORY SCHEMA
-- ============================================================
CREATE TABLE inventory.warehouses (
    id SERIAL PRIMARY KEY,
    name VARCHAR NOT NULL,
    location VARCHAR
);

CREATE TABLE inventory.products (
    id SERIAL PRIMARY KEY,
    name VARCHAR NOT NULL,
    price DECIMAL(10,2),
    metadata JSONB
);

CREATE TABLE inventory.stock_levels (
    warehouse_id INT REFERENCES inventory.warehouses(id),
    product_id INT REFERENCES inventory.products(id),
    quantity INT NOT NULL DEFAULT 0,
    PRIMARY KEY (warehouse_id, product_id)
);

-- ============================================================
-- SALES SCHEMA
-- ============================================================
CREATE TABLE sales.customers (
    id SERIAL PRIMARY KEY,
    name VARCHAR NOT NULL,
    email VARCHAR,
    region VARCHAR,
    created_at TIMESTAMP NOT NULL,
    favorite_order_id INT  -- FK added after orders table exists (creates cycle)
);

CREATE TABLE sales.orders (
    id SERIAL PRIMARY KEY,
    customer_id INT NOT NULL REFERENCES sales.customers(id),
    warehouse_id INT REFERENCES inventory.warehouses(id),
    ordered_at TIMESTAMP NOT NULL
);

-- Add the cycle-creating FK
ALTER TABLE sales.customers ADD CONSTRAINT fk_favorite_order
    FOREIGN KEY (favorite_order_id) REFERENCES sales.orders(id);

CREATE TABLE sales.order_lines (
    id SERIAL PRIMARY KEY,
    order_id INT NOT NULL REFERENCES sales.orders(id),
    product_id INT NOT NULL REFERENCES inventory.products(id),
    quantity INT NOT NULL,
    unit_price DECIMAL(10,2) NOT NULL
);

CREATE TABLE sales.order_transfers (
    id SERIAL PRIMARY KEY,
    from_order_id INT NOT NULL REFERENCES sales.orders(id),
    to_order_id INT NOT NULL REFERENCES sales.orders(id),
    reason VARCHAR NOT NULL
);

-- ============================================================
-- PUBLIC SCHEMA (passthrough + disconnected)
-- ============================================================
CREATE TABLE public.regions (
    code VARCHAR PRIMARY KEY,
    name VARCHAR NOT NULL,
    tax_rate DECIMAL(5,4)
);

CREATE TABLE public.feature_flags (
    key VARCHAR PRIMARY KEY,
    enabled BOOLEAN NOT NULL DEFAULT false,
    description TEXT
);

-- ============================================================
-- SEED DATA
-- ============================================================

-- Warehouses (5)
INSERT INTO inventory.warehouses (name, location) VALUES
    ('West Coast', 'Los Angeles'),
    ('East Coast', 'New York'),
    ('Central', 'Chicago'),
    ('South', 'Dallas'),
    ('Northwest', 'Seattle');

-- Products (20, some with JSONB metadata)
INSERT INTO inventory.products (name, price, metadata) VALUES
    ('Widget A', 9.99, '{"color": "red", "weight_kg": 0.5}'),
    ('Widget B', 14.99, '{"color": "blue", "weight_kg": 0.7}'),
    ('Gadget C', 24.99, NULL),
    ('Gadget D', 29.99, '{"color": "green"}'),
    ('Part E', 4.99, NULL),
    ('Part F', 7.49, '{"material": "steel"}'),
    ('Tool G', 49.99, '{"warranty_months": 12}'),
    ('Tool H', 59.99, '{"warranty_months": 24}'),
    ('Supply I', 2.99, NULL),
    ('Supply J', 3.49, NULL),
    ('Component K', 12.00, '{"spec": "v2"}'),
    ('Component L', 15.00, '{"spec": "v3"}'),
    ('Assembly M', 89.99, NULL),
    ('Assembly N', 99.99, NULL),
    ('Raw O', 1.50, NULL),
    ('Raw P', 2.00, NULL),
    ('Fixture Q', 34.99, '{"size": "large"}'),
    ('Fixture R', 39.99, '{"size": "medium"}'),
    ('Cable S', 6.99, NULL),
    ('Cable T', 8.99, NULL);

-- Stock levels (one entry per warehouse-product pair, subset)
INSERT INTO inventory.stock_levels (warehouse_id, product_id, quantity) VALUES
    (1, 1, 100), (1, 2, 200), (1, 5, 50),
    (2, 3, 150), (2, 4, 75), (2, 10, 300),
    (3, 7, 80), (3, 8, 60), (3, 15, 500),
    (4, 11, 120), (4, 12, 90), (4, 18, 40),
    (5, 6, 200), (5, 9, 350), (5, 20, 25);

-- Customers (10): IDs 1-5 created in 2024, IDs 6-10 created in 2025
INSERT INTO sales.customers (name, email, region, created_at) VALUES
    ('Alice', 'alice@example.com', 'US-W', '2024-06-15'),
    ('Bob', 'bob@example.com', 'US-E', '2024-07-20'),
    ('Carol', 'carol@example.com', 'US-C', '2024-08-10'),
    ('Dave', 'dave@example.com', 'US-S', '2024-09-05'),
    ('Eve', 'eve@example.com', 'US-W', '2024-10-30'),
    ('Frank', 'frank@example.com', 'US-E', '2025-01-15'),
    ('Grace', 'grace@example.com', 'US-C', '2025-02-20'),
    ('Hank', 'hank@example.com', 'US-S', '2025-03-10'),
    ('Iris', 'iris@example.com', 'US-W', '2025-04-05'),
    ('Jack', 'jack@example.com', 'US-E', '2025-05-30');

-- Orders (30, 3 per customer, spread across warehouses)
INSERT INTO sales.orders (customer_id, warehouse_id, ordered_at) VALUES
    (1, 1, '2024-07-01'), (1, 2, '2024-08-15'), (1, 3, '2024-09-01'),
    (2, 2, '2024-08-01'), (2, 3, '2024-09-15'), (2, 4, '2024-10-01'),
    (3, 3, '2024-09-01'), (3, 4, '2024-10-15'), (3, 5, '2024-11-01'),
    (4, 4, '2024-10-01'), (4, 5, '2024-11-15'), (4, 1, '2024-12-01'),
    (5, 5, '2024-11-01'), (5, 1, '2024-12-15'), (5, 2, '2025-01-01'),
    (6, 1, '2025-02-01'), (6, 2, '2025-03-15'), (6, 3, '2025-04-01'),
    (7, 2, '2025-03-01'), (7, 3, '2025-04-15'), (7, 4, '2025-05-01'),
    (8, 3, '2025-04-01'), (8, 4, '2025-05-15'), (8, 5, '2025-06-01'),
    (9, 4, '2025-05-01'), (9, 5, '2025-06-15'), (9, 1, '2025-07-01'),
    (10, 5, '2025-06-01'), (10, 1, '2025-07-15'), (10, 2, '2025-08-01');

-- Set favorite_order_id for some customers (creates cycle)
UPDATE sales.customers SET favorite_order_id = 1 WHERE id = 1;
UPDATE sales.customers SET favorite_order_id = 4 WHERE id = 2;
UPDATE sales.customers SET favorite_order_id = 16 WHERE id = 6;
UPDATE sales.customers SET favorite_order_id = 19 WHERE id = 7;

-- Order lines (90, 3 per order, referencing various products)
INSERT INTO sales.order_lines (order_id, product_id, quantity, unit_price) VALUES
    (1, 1, 2, 9.99), (1, 5, 1, 4.99), (1, 9, 3, 2.99),
    (2, 2, 1, 14.99), (2, 6, 2, 7.49), (2, 10, 1, 3.49),
    (3, 3, 1, 24.99), (3, 7, 1, 49.99), (3, 11, 2, 12.00),
    (4, 4, 3, 29.99), (4, 8, 1, 59.99), (4, 12, 1, 15.00),
    (5, 1, 1, 9.99), (5, 5, 2, 4.99), (5, 13, 1, 89.99),
    (6, 2, 2, 14.99), (6, 6, 1, 7.49), (6, 14, 1, 99.99),
    (7, 3, 1, 24.99), (7, 7, 2, 49.99), (7, 15, 3, 1.50),
    (8, 4, 1, 29.99), (8, 8, 1, 59.99), (8, 16, 2, 2.00),
    (9, 9, 5, 2.99), (9, 13, 1, 89.99), (9, 17, 1, 34.99),
    (10, 10, 2, 3.49), (10, 14, 1, 99.99), (10, 18, 1, 39.99),
    (11, 1, 3, 9.99), (11, 11, 1, 12.00), (11, 19, 2, 6.99),
    (12, 2, 1, 14.99), (12, 12, 2, 15.00), (12, 20, 1, 8.99),
    (13, 3, 2, 24.99), (13, 5, 1, 4.99), (13, 15, 1, 1.50),
    (14, 4, 1, 29.99), (14, 6, 3, 7.49), (14, 16, 2, 2.00),
    (15, 7, 1, 49.99), (15, 9, 2, 2.99), (15, 17, 1, 34.99),
    (16, 1, 2, 9.99), (16, 3, 1, 24.99), (16, 11, 1, 12.00),
    (17, 2, 1, 14.99), (17, 4, 2, 29.99), (17, 12, 1, 15.00),
    (18, 5, 3, 4.99), (18, 7, 1, 49.99), (18, 13, 1, 89.99),
    (19, 6, 2, 7.49), (19, 8, 1, 59.99), (19, 14, 1, 99.99),
    (20, 9, 1, 2.99), (20, 10, 2, 3.49), (20, 15, 1, 1.50),
    (21, 1, 1, 9.99), (21, 11, 3, 12.00), (21, 17, 1, 34.99),
    (22, 2, 2, 14.99), (22, 12, 1, 15.00), (22, 18, 2, 39.99),
    (23, 3, 1, 24.99), (23, 13, 1, 89.99), (23, 19, 1, 6.99),
    (24, 4, 3, 29.99), (24, 14, 1, 99.99), (24, 20, 2, 8.99),
    (25, 5, 1, 4.99), (25, 7, 2, 49.99), (25, 15, 3, 1.50),
    (26, 6, 1, 7.49), (26, 8, 1, 59.99), (26, 16, 1, 2.00),
    (27, 9, 4, 2.99), (27, 10, 1, 3.49), (27, 17, 2, 34.99),
    (28, 1, 1, 9.99), (28, 3, 2, 24.99), (28, 19, 1, 6.99),
    (29, 2, 3, 14.99), (29, 4, 1, 29.99), (29, 20, 1, 8.99),
    (30, 11, 1, 12.00), (30, 13, 2, 89.99), (30, 18, 1, 39.99);

-- Order transfers (8): exercises multi-FK to same table
-- Orders 16-30 belong to customers 6-10 (the imported set)
-- Transfers where BOTH orders are imported (should be included with AND semantics)
INSERT INTO sales.order_transfers (from_order_id, to_order_id, reason) VALUES
    (16, 17, 'warehouse change'),
    (20, 21, 'customer request'),
    (25, 28, 'consolidation');
-- Transfers where only ONE order is imported (should be excluded with AND semantics)
INSERT INTO sales.order_transfers (from_order_id, to_order_id, reason) VALUES
    (1, 16, 'partial from'),
    (16, 1, 'partial to'),
    (5, 20, 'partial from 2');
-- Transfers where NEITHER order is imported
INSERT INTO sales.order_transfers (from_order_id, to_order_id, reason) VALUES
    (1, 2, 'old transfer'),
    (3, 5, 'old transfer 2');

-- Regions (5, passthrough)
INSERT INTO public.regions (code, name, tax_rate) VALUES
    ('US-W', 'West', 0.0725),
    ('US-E', 'East', 0.0800),
    ('US-C', 'Central', 0.0650),
    ('US-S', 'South', 0.0600),
    ('US-NW', 'Northwest', 0.0700);

-- Feature flags (3, disconnected)
INSERT INTO public.feature_flags (key, enabled, description) VALUES
    ('dark_mode', true, 'Enable dark mode UI'),
    ('beta_search', false, 'New search algorithm'),
    ('export_csv', true, 'Allow CSV exports');
