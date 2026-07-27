# Master Data Management (MDM) Platform — Functional Requirements

*For evaluating alternative solutions to support current operational use cases.*

---

## Use Cases Overview

| # | Use Case | Description |
|---|---|---|
| **UC-1** | Automated entity ingestion | High-frequency automated ingestion of entity metadata from legacy systems, with data quality pipeline and mastering |
| **UC-2** | Governed metadata management | Semi-frequent, workflow-governed edits to classification/cataloguing metadata by business users |
| **UC-3** | Periodic reference data loads | Annual or rare bulk loads of domain-specific reference/forecast data |
| **UC-4** | Static lookup management | Very rarely changed lookup tables that map categories to geographic or operational groupings |

---

## Requirement Category 1: Data Ingestion & Integration

| ID | Requirement | Driven By |
|---|---|---|
| **DI-1** | Accept inbound data via **REST API** (JSON payload, POST with upsert semantics) from external ETL tools on a scheduled basis (e.g., every 10 minutes) | UC-1 |
| **DI-2** | Support **bulk CSV import** for initial loads and periodic refreshes | UC-3, UC-4 |
| **DI-3** | Authenticate inbound API callers via **token-based auth** with configurable session timeout | UC-1 |
| **DI-4** | Expose a **health-check endpoint** so upstream systems can verify availability before pushing data | UC-1 |
| **DI-5** | Accept **all-string input** and perform **type conversion** (string → integer, decimal, boolean, date) during the ingestion pipeline — don't require upstream systems to match the target schema exactly | UC-1 |

---

## Requirement Category 2: Data Quality Pipeline

| ID | Requirement | Driven By |
|---|---|---|
| **DQ-1** | Support a **multi-stage processing pipeline** (e.g., landing → staging → master) where data is progressively validated and enriched before being committed to the authoritative dataset | UC-1 |
| **DQ-2** | Perform **reference data lookups** during pipeline processing — resolve human-readable values (e.g., a name or code) into internal IDs by looking up against reference tables | UC-1 |
| **DQ-3** | Apply **business rules** during processing (e.g., default missing values, normalise data, flag anomalies) | UC-1 |
| **DQ-4** | Support **soft validation** — mark records with unresolvable references as invalid without blocking the entire pipeline; hold them for manual correction | UC-1 |
| **DQ-5** | Support **record-level dependency ordering** — child records that reference a parent entity must wait in staging until the parent exists in the master dataset | UC-1 |
| **DQ-6** | Support **event-driven chaining** — when data lands in one stage, automatically trigger the next processing step without manual intervention | UC-1 |
| **DQ-7** | Track **record history** (change log per record showing what changed, when, and by whom) | UC-1, UC-2 |

---

## Requirement Category 3: Data Modelling & Relationships

| ID | Requirement | Driven By |
|---|---|---|
| **DM-1** | Support **multiple distinct data domains** within the same platform, each with its own schema, access rules, and lifecycle (e.g., entity metadata vs. classification metadata vs. forecast data vs. lookup data) | All |
| **DM-2** | Support **foreign key relationships** between tables — both within and across data domains — with autocomplete/dropdown UI selection for FK fields | UC-1, UC-2 |
| **DM-3** | Support **association/junction tables** for many-to-many relationships (e.g., an entity can have multiple capabilities; a capability can belong to multiple entities) | UC-1 |
| **DM-4** | Support **enumerated/constrained fields** — restrict certain fields to a fixed set of valid values | UC-1, UC-2 |
| **DM-5** | Support **reference/lookup tables** that serve as the source of truth for the pipeline's validation and resolution logic (e.g., timezone codes, geographic regions, status values, classification categories) | All |

---

## Requirement Category 4: Governed Change Management

| ID | Requirement | Driven By |
|---|---|---|
| **GC-1** | Support **multi-step approval workflows** for data changes: editor proposes → documents rationale → approver reviews → approve/reject → changes merged or discarded | UC-2 |
| **GC-2** | All edits within a workflow must happen in **isolation** — other users should not see in-progress changes until they are approved and merged | UC-2 |
| **GC-3** | Rejected changes must be **fully discarded** with no residual impact on the authoritative dataset | UC-2 |
| **GC-4** | **Mandatory comments** at submission and review steps, captured as part of the audit trail | UC-2 |
| **GC-5** | **Full workflow history viewer** — show the complete decision chain (who did what, when, with what rationale) for any change request | UC-2 |
| **GC-6** | **Workflow administration** — admins can view active workflows, terminate stuck processes, reassign tasks between users | UC-2 |
| **GC-7** | **Workflow inbox** per user with badge counts showing pending tasks, and ability to claim or resume work items | UC-2 |

---

## Requirement Category 5: Notifications

| ID | Requirement | Driven By |
|---|---|---|
| **NT-1** | Send **email notifications** at key workflow transitions (e.g., submission for review, rejection) with deep-links back to the relevant record/task | UC-2 |
| **NT-2** | Support **different notification templates** per data domain (different recipients, subjects, content) | UC-2 |
| **NT-3** | Support configurable **service accounts** for sending notifications per environment (prod vs. non-prod) | UC-2 |

---

## Requirement Category 6: User Interface & Data Stewardship

| ID | Requirement | Driven By |
|---|---|---|
| **UI-1** | Provide a **web-based UI** for data stewards to view, search, and edit records | UC-1, UC-2 |
| **UI-2** | Support **role-based views** — different user roles see different subsets of data domains, tables, and available actions (e.g., editors only see their domain; admins see everything) | All |
| **UI-3** | Provide **FK-aware entry forms** — when editing a field that references another table, show a dropdown/autocomplete populated from the referenced table | UC-1, UC-2 |
| **UI-4** | Support **manual correction of pipeline failures** — when automated ingestion produces validation errors, data stewards can view and fix individual records via the UI | UC-1 |
| **UI-5** | Support **targeted record search** within tables | All |

---

## Requirement Category 7: Access Control & Security

| ID | Requirement | Driven By |
|---|---|---|
| **AC-1** | **Role-based access control** with at minimum: read-only, editor (via workflow only), approver, power user (direct edit bypassing workflow), and administrator | UC-1, UC-2 |
| **AC-2** | Integration with **enterprise identity provider** (e.g., AD/LDAP groups) for permission management | All |
| **AC-3** | **Granular permissions per data domain** — separate role assignments for different data domains (e.g., entity metadata editors ≠ classification metadata editors) | UC-1, UC-2 |
| **AC-4** | **Service account privilege elevation** — automated pipeline processes may need elevated permissions to write across data domains that no human user can directly access | UC-1 |

---

## Requirement Category 8: Downstream Data Distribution

| ID | Requirement | Driven By |
|---|---|---|
| **DD-1** | Mastered data must be accessible to downstream systems via **database views or replication** — preferably as materialized views in a relational database that downstream APIs and services can query directly | All |
| **DD-2** | Support **scheduled refresh** of downstream views (e.g., every 10 minutes for high-frequency domains) | UC-1 |
| **DD-3** | The platform's data store must be **queryable by external reporting/BI tools** (e.g., standard SQL against a PostgreSQL-compatible backend) | All |

---

## Requirement Category 9: Audit & Observability

| ID | Requirement | Driven By |
|---|---|---|
| **AO-1** | **Structured application logging** with multiple log streams (platform, data integration, custom logic) exportable to centralised log aggregation (e.g., ELK/Splunk) | All |
| **AO-2** | **Workflow audit trail** — immutable record of every workflow step, decision, user, timestamp, and comment | UC-2 |
| **AO-3** | **Data change audit** — ability to see what changed in a record, when, and by whom | UC-1, UC-2 |

---

## Requirement Category 10: Extensibility

| ID | Requirement | Driven By |
|---|---|---|
| **EX-1** | Support **custom processing logic** at data commit events (pre-commit for cleansing/defaults, post-commit for chaining subsequent processing) | UC-1 |
| **EX-2** | Support **custom data transformation functions** within the ingestion pipeline (not just type conversion — e.g., lookup-based resolution, conditional defaulting) | UC-1 |
| **EX-3** | Support **configurable field-to-field mapping** between source and target schemas during data transfer/transformation | UC-1 |

---

## Appendix: Implementation Artifacts vs. Genuine Requirements

The following items are specific to the current vendor's implementation approach. They are **not requirements** — they are one way of solving the above requirements. An alternative solution would achieve the same outcomes differently.

| Current Implementation Artifact | What It Actually Solves | Requirement ID |
|---|---|---|
| Dataspaces & branching/merging | Change isolation and governed merge | GC-2, GC-3 |
| Data Exchange (DEX) add-on | Field mapping & transformation between pipeline stages | EX-3, DQ-1 |
| Custom Java trigger classes | Event-driven pipeline chaining & business rules | EX-1, DQ-6 |
| Custom Java FK constraint class | Soft referential integrity validation | DQ-4, DM-2 |
| Perspectives (sidebar configurations) | Role-based UI views | UI-2 |
| Historization toggle | Record-level change tracking | DQ-7 |
| 250-char string limit on landing fields | A vendor-specific constraint for historization — not a real need | *N/A — artifact only* |
| Custom privilege elevation handler | Service account cross-domain writes | AC-4 |

---

## Summary

This document captures **39 functional requirements** across **10 categories** derived from **4 active use cases**. The requirements range from high-frequency automated data ingestion with quality pipelines (UC-1) to static lookup table management (UC-4).

A replacement solution does not necessarily need to be a monolithic MDM platform. The use cases could potentially be decomposed:

- **UC-1** (automated ingestion + quality pipeline + mastering) is the most demanding and drives the majority of requirements
- **UC-2** (governed change management) drives the workflow, approval, and notification requirements
- **UC-3 & UC-4** (periodic/static reference data) have minimal requirements and could be served by simpler tooling

Any evaluation should prioritise UC-1 and UC-2 capability, as these represent the core ongoing operational need.
