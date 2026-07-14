CREATE TABLE sessions (
    id TEXT PRIMARY KEY,
    cwd TEXT,
    title TEXT,
    custom_title TEXT,
    created_at INTEGER,
    last_activity_at INTEGER,
    deleted_at INTEGER
);

CREATE TABLE workspaces (
    path TEXT PRIMARY KEY,
    name TEXT,
    last_opened_at INTEGER
);
