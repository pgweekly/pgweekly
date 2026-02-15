# SQL Property Graph Queries (SQL/PGQ): Bringing Graph Queries to PostgreSQL

## Introduction

In February 2024, Peter Eisentraut announced a prototype implementation of **SQL Property Graph Queries (SQL/PGQ)** on the pgsql-hackers mailing list—a new way to run graph-style queries directly in PostgreSQL, following the SQL:2023 standard (ISO 9075-16). The initiative had been briefly discussed at the FOSDEM developer meeting, and community interest led Peter to share his work-in-progress.

Nearly two years later, the patch has evolved into a substantial implementation: **118 files changed**, ~**14,800 lines** added. Peter and Ashutosh Bapat are the primary authors, with Junwang Zhao reviewing and Ajay Pal and Henson Choi testing. The latest iteration (v20260113) consolidates features from v0 through v14 and beyond—including cyclic path patterns, access permissions, RLS support, graph element functions (`LABELS()`, `PROPERTY_NAMES()`), multi-pattern path matching, ECPG support, property collation rules, and `pg_overexplain` integration.

SQL/PGQ lets you define property graphs over existing relational tables and query them using path patterns (vertices connected by edges), much like Cypher or GQL. Unlike dedicated graph databases, this approach maps graphs onto the relational model: graphs are *views* over tables, and graph queries rewrite to joins and unions. The discussion raised important architectural questions about *when* and *how* that transformation should happen—and whether the rewriter is the right place for it.

## Why This Matters

Many applications have naturally graph-shaped data: social networks, supply chains, recommendation systems, fraud detection. Today, developers either:

- Use a separate graph database (Neo4j, etc.) and maintain two systems, or
- Encode graph traversals as recursive CTEs and complex joins in PostgreSQL.

SQL/PGQ aims to give PostgreSQL users a standard, declarative way to express graph queries without leaving SQL or duplicating data. The standard has been adopted by Oracle 23c and others; bringing it to PostgreSQL would improve interoperability and make graph capabilities available to a broad user base.

## Technical Analysis

### The SQL/PGQ Model

A **property graph** in SQL/PGQ is a virtual structure defined over existing tables:

- **Vertices** are rows from one or more tables (with optional labels).
- **Edges** are relationships, typically inferred from foreign keys or explicitly specified.
- **Properties** are columns from those tables.
- **Labels** can be shared across multiple element tables (e.g., `person` for both `customers` and `employees`), with each label exposing its own set of properties.

You create a graph with `CREATE PROPERTY GRAPH` and query it with `GRAPH_TABLE(... MATCH ... COLUMNS ...)`. Example:

```sql
CREATE PROPERTY GRAPH myshop
    VERTEX TABLES (
        products LABEL product,
        customers LABEL customer,
        orders LABEL "order"
    )
    EDGE TABLES (
        order_items SOURCE orders DESTINATION products LABEL contains,
        customer_orders SOURCE customers DESTINATION orders LABEL has_placed
    );

SELECT customer_name FROM GRAPH_TABLE (myshop
  MATCH (c IS customer)-[IS has_placed]->(o IS "order" WHERE o.ordered_when = current_date)
  COLUMNS (c.name AS customer_name));
```

The `MATCH` clause describes a path pattern; the implementation rewrites it to joins and filters that PostgreSQL can plan and execute.

### Implementation Approach: Rewrite System

The implementation uses the **rewrite system** for graph-to-relational transformation—the same stage where views expand. Peter explained that this aligns with the SQL/PGQ specification: graphs map to relations, and queries expand like view definitions. By the time the planner sees the query, the graph structure has been flattened into standard relational form, keeping the implementation consistent with view security (privileges, security barriers).

## Patch Evolution: From v0 to v20260113

### v0: Fragile Prototype (Feb 2024)

The initial patch was ~332 KB. Peter described it as "quite fragile." It introduced `CREATE PROPERTY GRAPH`, `GRAPH_TABLE`, basic path patterns, and documentation in `ddl.sgml` and `queries.sgml`.

### v1 and Early Refinements (June–Aug 2024)

Peter and Ashutosh released v1 with a "fairly complete minimal feature set." Ashutosh contributed:

- **WHERE clause in graph patterns**—e.g., `MATCH (a)->(b)->(c) WHERE a.vname = c.vname`
- **Spurious "column not found" bug fix**: Attribute names were referenced from `pg_attribute` heap tuples; after `RELCACHE_FORCE_RELEASE` they could point to freed memory. The fix was to copy attribute names.
- Compilation fixes, pgperltidy compliance, error location reporting for implicit properties/labels.

Imran Zaheer added support for **EXCEPT lists** in labels and properties.

### v14: Cyclic Paths, Access Permissions, RLS (Aug–Oct 2024)

Ashutosh contributed major features:

- **Cyclic path patterns**: Path patterns where an element variable repeats (e.g., same vertex at both ends of a path). Elements sharing a variable must have the same type and label expression; repeated edge patterns are not supported.
- **Access permissions on property graphs**: The property graph acts like a **security invoker view**—the current user must have privileges on the underlying tables. Queries succeed only for elements the user can access. Security definer property graphs are not implemented.
- **Row Level Security (RLS)**: Regression tests in `graph_table_rls.sql` verify RLS behavior with property graphs.
- **Property collation and edge-vertex links**: Same-named properties across elements must have the same collation. Edge key columns and referenced vertex keys must have compatible collations. Edge-vertex link quals use equality operators, with dependencies so they cannot be dropped without dropping the edge.
- **`\d` and `\dG` variants**: `\d` on a property graph shows elements, tables, kinds, and end-vertex aliases; `\d+` adds labels and properties. `\dG` lists property graphs; `\dG+` adds owner and description.

### Henson Choi: LABELS(), PROPERTY_NAMES(), Multi-Pattern (Dec 2025)

Henson Choi contributed three patches:

- **LABELS() graph element function**: Returns all labels of a graph element as `text[]`. Implemented by wrapping element tables in subqueries with a virtual `__labels__` column, enabling the planner to prune Append branches when filtering by labels (e.g., `WHERE 'Person' = ANY(LABELS(v))`).
- **PROPERTY_NAMES() graph element function**: Returns all property names as `text[]`, with similar planner pruning for property-based filters.
- **Multi-pattern path matching**: Support for comma-separated path patterns in `MATCH`, e.g., `MATCH (a)->(b), (b)->(c)`. Patterns with shared variables merge into one join; disconnected patterns produce a Cartesian product (aligned with SQL/PGQ and Neo4j Cypher).

### v20260113: Consolidated Implementation (Jan 2026)

The latest patch (v20260113) merges all prior work into a single WIP patch:

- **ECPG support**: SQL/PGQ in ECPG—basic queries, prepared statements, cursors, dynamic queries. Label disjunction in ECPG required changes to the ecpg lexer.
- **pg_overexplain integration**: Property graph RTEs and `RELKIND_PROPGRAPH` are recognized for `EXPLAIN (RANGE_TABLE, ...)`.
- **Extended test coverage**: `create_property_graph.sql` (365 lines), `graph_table.sql` (561 lines), `graph_table_rls.sql` (363 lines), `privileges.sql` (58 lines).
- **rewriteGraphTable.c**: Grown from ~420 to ~1,330 lines; `propgraphcmds.c` from ~1,000 to ~1,860 lines.

## Community Insights

### Andres Freund: Concerns About the Rewriter

Andres Freund raised a structural concern: transformation via the rewrite system bars the planner from benefiting from graph semantics and increases rewrite-system usage. Peter responded that PGQ is designed as relational-at-core (like view expansion), and the standard and other implementations follow this model. Tomas Vondra wondered whether retaining graph structure longer could enable graph-specific indexes or executor nodes; Ashutosh Bapat noted that many optimizations would improve the underlying joins anyway, and aligning with view expansion makes sense for security.

### Florents Tselai: Documentation

Florents Tselai suggested reordering the docs to answer *"I have some data modeled as a graph G=(V, E). Can Postgres help me?"* first, and to use runnable examples from `graph_table.sql`. He compared with Apache Age’s jsonpath-style approach but agreed the standard’s relational mapping fits core PostgreSQL.

## Technical Details

### Architecture

- **Parser**: Grammar for `CREATE PROPERTY GRAPH`, `ALTER PROPERTY GRAPH`, `DROP PROPERTY GRAPH`, and `GRAPH_TABLE(... MATCH ... COLUMNS ...)`.
- **Catalogs**: `pg_propgraph_element`, `pg_propgraph_element_label`, `pg_propgraph_label`, `pg_propgraph_label_property`, `pg_propgraph_property`.
- **Rewrite**: `rewriteGraphTable.c` transforms graph patterns into joins and unions.
- **Utilities**: `pg_dump`, `psql` `\d`/`\dG`, tab completion; `pg_get_propgraphdef()` for introspection.
- **ECPG**: Full support in embedded SQL.

### Access Control

1. A user needs **SELECT** on the property graph.
2. The property graph is **security invoker**: the current user must have privileges on the underlying tables. Queries only succeed for elements the user can access.
3. In a security definer view, property graph access uses the view owner’s privileges; base relation access uses the executing user’s privileges.
4. Security definer property graphs are not implemented (the standard does not mention them).

### Property Collation and Edge-Vertex Links

- Properties with the same name must have the same collation across elements.
- Edge key and vertex key collations must match when keys are explicitly specified (foreign-key-derived links rely on the constraint).
- Edge-vertex link quals use equality operators; the edge depends on those operators so they cannot be dropped independently.

## Current Status

The **v20260113** patch is a consolidated WIP. It includes:

- Full `CREATE PROPERTY GRAPH` / `ALTER` / `DROP` with labels, properties, KEY clauses, SOURCE/DESTINATION REFERENCES
- `GRAPH_TABLE` with path patterns, WHERE in patterns, cyclic paths, multi-pattern
- `LABELS()` and `PROPERTY_NAMES()` graph element functions
- Access permissions, RLS, privileges tests
- ECPG support, pg_overexplain integration
- Documentation and regression tests

The patch is **not yet committed**. Peter and Ashutosh continue to refine it; the rewriter-based design remains the chosen approach, with ongoing review and testing.

## Conclusion

SQL/PGQ would bring standardized graph query syntax to PostgreSQL. The implementation has grown from a fragile prototype to a feature-rich patch with cyclic paths, graph element functions, multi-pattern matching, access control, RLS, ECPG support, and comprehensive tests. The main architectural choice—rewriting graph queries to relations in the rewrite system—aligns with the standard and view semantics. If committed, PostgreSQL users could express path patterns over relational data without leaving SQL or maintaining a separate graph database.

## References

- [Discussion thread: SQL Property Graph Queries (SQL/PGQ)](https://www.postgresql.org/message-id/flat/a855795d-e697-4fa5-8698-d20122126567%40eisentraut.org)
- [Oracle 23c: SQL Property Graphs and SQL/PGQ](https://oracle-base.com/articles/23c/sql-property-graphs-and-sql-pgq-23c)
- [CIDR 2023: DuckPGQ](https://www.cidrdb.org/cidr2023/papers/p66-wolde.pdf)
- [PGCon 2019: Graph databases in PostgreSQL](https://www.pgcon.org/2019/schedule/events/1300.en.html)
