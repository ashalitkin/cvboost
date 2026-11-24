CREATE TABLE example_log (
    id INTEGER PRIMARY KEY,
    item_id INTEGER NOT NULL REFERENCES example_items(id),
    note TEXT
);
