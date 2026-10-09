-- Tiny database for the agent-loop tests (tests/agent), built into tmp_path at test time and served
-- by a real MCP server subprocess as database id `fixture`.
CREATE TABLE team (id INTEGER PRIMARY KEY, name TEXT NOT NULL, city TEXT);
CREATE TABLE player (id INTEGER PRIMARY KEY, name TEXT NOT NULL, team_id INTEGER REFERENCES team(id), goals INTEGER);
INSERT INTO team VALUES (1, 'Lions', 'Lyon'), (2, 'Bears', 'Bern'), (3, 'Owls', 'Oslo');
INSERT INTO player VALUES
  (1, 'Ana', 1, 12),
  (2, 'Ben', 1, 3),
  (3, 'Cy', 2, 7),
  (4, 'Dee', 2, NULL),
  (5, 'Eve', 3, 9);
