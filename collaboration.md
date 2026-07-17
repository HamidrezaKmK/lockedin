# Workspace-based collaboration

## Decision

Replace user-owned research libraries and the proposed per-bubble collaboration ACL with a
first-class **workspace**. A workspace, not an account, owns every research artifact:

- PDFs and their extracted text, metadata, summaries, bibliography, tags, and attention state;
- bubbles and their paper membership, pages, figures, chats, and public-share settings; and
- TODOs and the `@<id>` references that connect them to report pages.

An account can belong to many workspaces, and a workspace can have many account members. Every
approved account has exactly one Personal workspace, created with the account. Existing users
receive one during migration. The product opens that Personal workspace by default; switching a
workspace changes the complete library, bubbles, TODOs, attention queue, and research context
shown by the application.

This deliberately replaces—not augments—the old `owner/bubble` model in this document. There is
no owner-backed bubble filesystem, no copying editor contributions into another user's library,
and no separate shared-TODO scope. A bubble cannot cross workspace boundaries, so a paper,
figure, TODO, page, or chat is always resolved in one unambiguous workspace.

## Terms and invariants

| Term | Meaning |
| --- | --- |
| Account | Authentication identity, personal preferences, model credentials, entitlement, and linked Slack identities. It does not own research content. |
| Workspace | A stable UUID-backed research container with a display name, members, and all research data. |
| Personal workspace | The one private workspace automatically created for an account. It has that account as its only member and cannot be shared, transferred, or deleted while the account exists. Its display name is `Personal` (not the username). |
| Shared workspace | A workspace intentionally created for collaboration. It can have multiple members and is independent of any one member's personal library. |
| Workspace owner | The administrative member responsible for a shared workspace, not the owner of individual artifacts. Ownership can be transferred. |
| Member | An approved account with a workspace role. `admin` can manage membership and workspace lifecycle; `editor` can read and change all workspace research content. There is no viewer role in v1. |

Required invariants:

1. Each research object is stored below exactly one `workspace_id`; no object carries a user as
   its authorization or storage owner. Optional `created_by`/`updated_by` audit fields are only
   attribution.
2. Every authenticated read and write resolves `(actor, workspace_id)` through one membership
   helper before touching disk or invoking a service. Never derive a workspace from a supplied
   username or trust a client-provided path.
3. A bubble references only assets and TODOs in its own workspace. Asset IDs and TODO IDs need
   only be unique inside their workspace.
4. Deleting or removing a member never deletes their contributions; they already belong to the
   workspace. Removal immediately prevents subsequent web, Scientist, and Slack access.
5. Public share links remain unlisted and read-only. A link identifies `(workspace_id,
   bubble_slug)`, is checked against the bubble's current share flag, and grants no workspace
   membership.

## Storage and registry

Use opaque immutable UUIDs as workspace identifiers. Names and usernames are mutable and must
not appear in authorization keys or filesystem paths.

```text
data/
  users/
    accounts.yaml                 # account records and personal preference only
    <username>/
      config/                     # that user's model keys, active provider, and visual preferences
  workspaces/
    workspaces.yaml               # workspace registry and membership index
    <workspace-uuid>/
      ASSETS/
      REPORTS/
      bubbles.yaml
      todos.yaml
      config/
        math.yaml                 # workspace-wide KaTeX macro definitions
```

`workspaces.yaml` is the authoritative registry, written atomically with restrictive permissions.
It contains at least:

```yaml
version: 1
workspaces:
  "7c...":
    id: "7c..."
    name: "Personal"
    kind: personal                 # personal | shared
    owner_user: alice              # administrative owner; required for shared, fixed for personal
    created_at: "..."
    updated_at: "..."
    members:
      alice: {role: admin, joined_at: "..."}
```

Store `personal_workspace_id` in each account record. Keep the browser's current workspace in
its route/tab state, not as a cross-device account preference: opening the root application must
always start in Personal. Keep all private model configuration under the account directory. Do
not put API keys, session data, Slack credentials, premium entitlements, or account-management
settings in a shared workspace.
Math macros belong to the workspace: every report preview, editor, public share, Scientist
mirror, and member of that workspace uses the same `config/math.yaml`. Visual themes remain
personal preferences in v1. This provides consistent report syntax and rendering without sharing
credentials or other account-private configuration.

Refactor `paths` around `workspace_home(workspace_id)` and `use_workspace_root(...)`; service
functions take a resolved workspace object/root rather than `home_of(user)`. Keep `user_home`
only for account-private settings during the transition. Background tagging jobs must carry the
workspace ID captured at enqueue time and re-check that the job's workspace still exists before
writing results. Update all math-config path helpers to resolve against the active workspace root,
not the user root.

## Authorization and lifecycle

Implement one workspace repository/service with these operations:

- `create_personal_workspace(user)` — idempotent, invoked by account creation and migration.
- `list_memberships(user)` — returns only workspaces the approved account may access, including
  ID, name, kind, role, owner display name, and a lightweight updated timestamp.
- `resolve_membership(user, workspace_id, minimum_role="editor")` — validates account approval,
  workspace existence, membership, and role. It is the only gateway to workspace storage.
- `create_shared_workspace(actor, name)`, `rename_workspace`, `invite_member`, `revoke_member`,
  `leave_workspace`, `transfer_ownership`, and `delete_workspace`.

Rules:

- Any approved user may create a shared workspace and becomes its admin/owner. Require a
  nonempty, bounded display name; names need not be globally unique.
- Invitees must be existing approved accounts. Inviting a current member is idempotent or returns
  a clear `409`; self-invites are rejected. v1 invitations take effect immediately, matching the
  existing trusted-account model; an invitation table/accept flow can be added later without
  changing artifact ownership.
- Shared-workspace admins can invite/revoke editors, promote/demote admins, rename the
  workspace, transfer ownership to another admin, and delete it. Editors may change all research
  content but cannot alter membership, name, ownership, or deletion.
- Do not allow removal or demotion of the sole admin. An owner may leave only after transferring
  ownership. A personal workspace always has exactly its account holder as admin; it cannot be
  shared, left, transferred, renamed through collaboration controls, or deleted independently.
- Account rename changes account records and membership keys atomically; it does **not** move
  workspace directories or invalidate public links. Account deletion removes all memberships,
  revokes sessions/Scientist tokens/Slack links, and requires a policy decision in the UI for any
  shared workspaces it solely administers: transfer to a nominated admin or delete them. Personal
  workspace data is deleted only as part of confirmed account deletion.
- Workspace deletion is an admin-only, explicit destructive action. It removes its directory and
  public-share entries only after confirmation; it never touches account-private settings.

Existing page-save optimistic concurrency remains the v1 editing model: refresh remote changes
when the local editor is clean and return an explicit `409` on a stale save. Do not promise CRDTs,
live cursors, or silent last-writer-wins behavior.

## Web API and routing

Make the workspace explicit in every research endpoint. Prefer a uniform route shape:

```text
/api/workspaces
/api/workspaces/{workspace_id}
/api/workspaces/{workspace_id}/members
/api/workspaces/{workspace_id}/assets/...
/api/workspaces/{workspace_id}/bubbles/...
/api/workspaces/{workspace_id}/todos/...
/api/workspaces/{workspace_id}/settings/math
```

All existing assets, attention, bubbles, bubble pages/figures/chats/preview/poll, and TODO routes
must be moved or internally routed through this scope. The server obtains the workspace from the
path, calls `resolve_membership`, then passes only that workspace root to `service`, `assets`,
`bubbles`, `todos`, `tagger`, report rendering, and file serving. A URL may be bookmarked only
when its viewer still has membership; otherwise return `403` rather than falling back to a
Personal workspace.

Provide `GET /api/workspaces`; it identifies the caller's Personal workspace and memberships.
On login and whenever `lockedin.codes/` is opened without a workspace route, the frontend must
select the Personal workspace. A workspace route is allowed only after validating membership.
The frontend must load this selection before fetching assets or bubbles, include its immutable
workspace ID in all research requests, and reset in-memory state (open bubble, page, chat, TODO
list, asset filters, polling, and unsaved-editor guard) when the selection changes. If an editor
is dirty, require save, discard, or cancel before switching.

The top-right identity control changes from a username display to a **workspace switcher**. It
shows the active workspace name (for example, `Personal` or `Quantum Group`) and exposes the
memberships list, role, and “Manage workspaces” entry. Account/profile actions remain available
from Settings, not as the active-context label. Add a Settings → Workspaces panel with:

- current workspace and role;
- switcher, workspace creation, and membership list;
- admin-only invite, role, revoke, transfer, rename, and delete controls; and
- a clear indication that Library, Bubbles, TODOs, and chat are workspace-wide.

Add a separate **Workspace settings** section alongside normal account Settings. It contains the
active workspace's math-macro editor and an explanation that changes apply to every member and
every bubble in that workspace. The macro API must be workspace-qualified and use
`resolve_membership`; editors may update macros because they are report-authoring data, while
workspace-admin-only settings (membership, name, ownership, deletion) remain in the management
controls. On a workspace switch, reload the macro map before rendering or editing a report; do
not retain macros from the previous workspace. Public previews load the workspace macro file, so
their rendering matches authenticated members.

Workspace Settings must also show a **Members** list to every workspace member. Each entry shows
the account username, role (`admin` or `editor`), and, where useful, the workspace owner marker.
For workspace admins, the same panel provides an “Add member” control that accepts an existing
approved LockedIn username, plus per-member controls to promote an editor to admin, demote an
admin to editor, revoke access, and transfer ownership. Hide or disable these controls for
editors. Enforce the server-side rules as well: an admin cannot remove/demote the sole admin,
and ownership can be transferred only to an existing admin. Refresh the roster after every
membership change and clear a removed user's active workspace in all clients on their next
authorized request.

The main application should open directly into the selected Personal workspace for a new user,
with Library as today. Switching then makes Assets, TODOs, Bubbles, attention, bubble chat, and
new uploads operate in the chosen workspace. Existing hash routes must be upgraded to retain the
workspace ID (for example `#/w/<id>/assets` and `#/w/<id>/bubbles/<slug>`), with redirects from
old same-user links during the compatibility period.

Public URLs remain separate, such as `/share/{token}`. Their resolver looks up workspace ID and
bubble slug in the share index, uses the workspace root, and has no user/session dependency.
Preview HTML must generate workspace-qualified asset and internal preview URLs. Public share
pages render only TODOs from that same workspace and must not expose member names, account
settings, other bubbles, or arbitrary assets.

## Data behavior inside a workspace

- Uploads, URL imports, PDF extraction, summaries, metadata edits, deletion, tagging, and the
  attention queue are workspace operations. A newly uploaded PDF is visible to all members.
- Bubbles are workspace-wide. Any member may create, approve, rename, attach/remove papers,
  edit pages, upload figures, manage chats, and toggle an unlisted share link. Bubble deletion
  requires workspace admin because it is destructive to shared work; if product policy later
  allows editor deletion, make that an explicit role change.
- TODOs are a single workspace-wide `todos.yaml`; `@id` resolves only there. Reference scans and
  ID compaction scan every bubble in the same workspace, never another workspace. This prevents
  the current global-per-user TODO ambiguity.
- Asset membership, report figures, chats, and page links never accept filesystem paths from the
  client. Validate identifiers and keep existing basename/path-traversal protections.
- Add optional activity metadata (`created_by`, `updated_by`, timestamps) to new artifacts and
  mutation events. It is audit/UI information, not authorization. Backfill legacy objects with
  the migrated account as `created_by` only where useful; do not fabricate precision.

## Migration of every existing account

Ship this as a versioned, resumable migration run before serving normal research requests. Back
up `data/users/`, take a single-process maintenance lock, and record per-account progress in a
migration journal so restart is safe.

For each account, including pending accounts:

1. Create its Personal workspace (or reuse the recorded ID) and add exactly that account as
   admin.
2. Move the research roots from `data/users/<username>/` into
   `data/workspaces/<workspace-id>/`: `ASSETS/`, `REPORTS/`, `bubbles.yaml`, `todos.yaml`, and
   `config/math.yaml`.
   Preserve bytes, mtimes where practical, existing bubble slugs, page manifests, assets, chats,
   TODO IDs, and share tokens.
3. Leave account-private configuration in the user directory, except move `config/math.yaml` to
   the Personal workspace. Initialize absent workspace content directories/files idempotently.
4. Rewrite the global share index from `(username, bubble_slug)` to
   `(workspace_id, bubble_slug)` without changing tokens. Verify every active token resolves.
5. Set `personal_workspace_id` to the new Personal workspace. Update any queued job records to
   their workspace ID.
6. Validate the destination: asset count/IDs, bubble slugs, report-file hashes, TODO data, and
   public-link resolution must match the source. Mark the journal entry complete only then.

Use rename-on-the-same-volume where possible, otherwise copy to a staging directory, verify, then
atomically promote. Do not delete the legacy source until verification succeeds; retain a
read-only backup until the release rollback window closes. A failed migration must resume from
the journal and never create a second Personal workspace or duplicate assets. After all accounts
are complete, remove the old user-root fallback and legacy routes in a later release rather than
maintaining two writable sources of truth.

## LockedIn Scientist

Scientist authorization remains tied to an account token, but every content operation is tied to
an explicitly selected workspace. The client keeps `active_workspace_id` in its local account
state (not in the server's web preference), so a terminal selection never changes the browser or
Slack selection.

Add:

```bash
lockedin-scientist workspaces
lockedin-scientist switch <workspace-id-or-unambiguous-name>
lockedin-scientist bubbles
lockedin-scientist sync [--workspace <id>]
lockedin-scientist codex <bubble-slug>
lockedin-scientist claude <bubble-slug>
lockedin-scientist agy <bubble-slug>
```

`switch` validates membership with the server, stores the UUID locally, and prints the selected
workspace name and role. Login selects the Personal workspace initially. `bubbles` lists only
the active workspace's approved bubbles and identifies the active workspace in its heading.
`sync` and all agent commands fail with an actionable message if no active workspace is set;
they never aggregate same-slug bubbles across workspaces. For automation, `--workspace` is an
ephemeral override and does not persist unless `switch` is used.

Replace the current whole-user Scientist filesystem protocol with workspace-scoped endpoints,
for example `/api/scientist/v1/workspaces/{id}/manifest`, `/files`, `/push`, `/pages`, and
`/bubbles`. Each calls `resolve_membership` for the token user on every request. The manifest
contains only that workspace's allowed research files—approved bubble pages and figures plus
their paper context, workspace TODO context, and workspace math configuration—and never any
account config, credentials, sessions, other workspace files, or chats. Keep revision-guarded writes and re-check membership
on push; revocation blocks future pull/push immediately.

Store mirrors below a UUID namespace, e.g.
`~/.lockedin-scientist/mirrors/<server-hash>/<workspace-uuid>/REPORTS/...`, not a user/bubble
path. Update agent instructions and generated markdown URLs to workspace-qualified API routes.
Migrate existing local mirror state once: map it to the account's Personal workspace after server
confirmation, or leave it untouched and require a clean first sync if confirmation is impossible.

## Slack bot

Slack sessions remain authenticated as an account, but bot state gains an active workspace before
the active bubble. Persist that choice per `(linked Slack user, server)` (or restore Personal on
first login); do not reuse the web or Scientist selection.

Add `workspaces` to list accessible workspaces and `switch workspace` to show numbered choices;
accept a number, validate membership through the server, set the Slack active workspace, and
clear the active bubble, TODO wizard, and any pending bubble selection. Include the active
workspace in the help/status message. Keep `select`/`switch bubble` for bubbles inside the active
workspace only.

All bot API calls—upload/link import, assets, attention, bubbles, ask/chat, and TODO CRUD—must
use the selected workspace ID. If the selection becomes invalid after revocation, clear it,
inform the user, and offer their Personal workspace. File uploads must never silently land in a
different workspace. The bot's workspace switch must be available in both DM and mention flows.

## Tests, rollout, and documentation

Add deterministic coverage for:

- account creation and migration creating one Personal workspace per existing/new account;
- isolation: same bubble slug, asset ID, and TODO ID in two workspaces never leak or collide;
- membership authorization for every research endpoint, direct URL, background job, Scientist
  endpoint, and Slack call; editor versus admin actions; revoke/leave/transfer/last-admin cases;
- migration preservation of files, metadata, TODO references, chats, and stable public share
  tokens; workspace math macros; restart/resume and rollback safety;
- workspace switching UX, dirty-editor protection, stale saved selection, initial Personal
  redirect, and route/bookmark behavior;
- shared workspace editing, page conflicts, asset ingestion, tagging, TODO reference deletion,
  figures, public previews, shared math macros, and account rename/deletion effects;
- Scientist workspace listing/switching, isolated mirrors, scoped manifests, ambiguous name
  handling, `--workspace`, and revoked-token behavior; and
- Slack workspace selection, selection reset, workspace-scoped upload/TODO/question behavior,
  and revoked membership recovery.

Deploy behind maintenance mode: back up, migrate, run integrity checks, then enable workspace
routes and clients. Release the server before web, Slack, and Scientist updates; retain
read-only compatibility redirects long enough for installed Scientist clients to upgrade. Update
README, landing copy, CLI help/install instructions, Slack setup, API documentation, and all UI
language that currently calls a user directory or a bubble a “workspace.”

## Explicit non-goals for v1

- character-level coediting, CRDTs, presence indicators, and live cursors;
- cross-workspace asset linking or moving individual bubbles between workspaces;
- public workspace membership or anonymous write access;
- sharing account model keys, personal settings, or private configuration; and
- a viewer/commenter permission tier or invitation acceptance workflow.

These can be added later on the stable workspace identity and membership boundary above.
