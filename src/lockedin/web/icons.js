/* LockedIn icon set — one geometric family, drawn on a 24×24 grid.
 *
 * Why this exists: the UI used OS emoji for its whole iconography (🏠 🫧 📚 ✅ 🗂️ ⚙️ 🤖 …).
 * Those render differently on every platform, arrive in colours the palette never declared, and
 * cannot follow a theme. These are strokes that inherit `currentColor`, so one icon is correct in
 * all five themes and on every device.
 *
 * Two registers, deliberately:
 *   - UI icons  — precise geometry, 1.7px stroke, round caps. Navigation, actions, status.
 *   - Marks     — the five chalk-talk verdicts (wrong / unclear / deeper / keep / cut) plus ink.
 *                 Drawn slightly off-true, the way a person marks a page. They are the one place
 *                 the interface is allowed to look hand-made, matching the landing demo board.
 *
 * Usage:  LIIcon("home")            -> <svg> element
 *         LIIcon("home", {size:18}) -> sized
 *         LIIcon.html("home")       -> markup string, for innerHTML/template contexts
 * The sprite is injected once; every call is a 40-byte <use>.
 */
(function () {
  "use strict";

  // Paths are authored as arrays so a single icon can mix strokes and solids without the
  // caller caring. A plain string means "stroked path"; {d,fill:1} means "solid".
  var P = {
    /* ---------- brand & navigation ---------- */
    // The brand mark: a padlock held between two angle brackets. The brackets stay outside the
    // lock — inside the body they had to share eight pixels of height with the keyhole at UI
    // sizes and turned to mush. Both sit on the same centre line: the lock's full span, shackle
    // top to body bottom, is 5.6..18.4, which centres on 12 like the brackets' apexes. The
    // shackle is a squared arch over a deep body, because a shallow arc on a wide shallow box
    // reads as a shopping bag. Keep this drawing and the two favicons in step.
    lock: ["M4.6 5.6 1.8 12l2.8 6.4",
           "M19.4 5.6 22.2 12l-2.8 6.4",
           "M9.9 9.9V6.9a1.3 1.3 0 0 1 1.3-1.3h1.6a1.3 1.3 0 0 1 1.3 1.3v3",
           "M9.6 9.9h4.8a1.7 1.7 0 0 1 1.7 1.7v5.1a1.7 1.7 0 0 1-1.7 1.7H9.6a1.7 1.7 0 0 1-1.7-1.7v-5.1a1.7 1.7 0 0 1 1.7-1.7Z",
           {d: "M12 12.75a1.25 1.25 0 0 1 .75 2.25v1.55a.75.75 0 0 1-1.5 0v-1.55a1.25 1.25 0 0 1 .75-2.25Z", fill: 1}],
    home: ["M3.6 10.4 12 3.5l8.4 6.9v8.4a1.6 1.6 0 0 1-1.6 1.6H5.2a1.6 1.6 0 0 1-1.6-1.6Z",
           "M9.4 20.4v-5.6h5.2v5.6"],
    // Three overlapping circles — the bubble cluster, kept from 🫧 but drawn to the grid.
    bubbles: ["M9.1 20.4a5.9 5.9 0 1 1 0-11.8 5.9 5.9 0 0 1 0 11.8Z",
              "M16.9 10.6a3.6 3.6 0 1 1 0-7.2 3.6 3.6 0 0 1 0 7.2Z",
              "M18.7 19.9a2.2 2.2 0 1 1 0-4.4 2.2 2.2 0 0 1 0 4.4Z"],
    // A stack seen edge-on: papers, not books. Reads at 16px where a book spine does not.
    library: ["M12 3.4 20.6 7.7 12 12 3.4 7.7Z", "M3.4 12 12 16.3 20.6 12", "M3.4 16.3 12 20.6l8.6-4.3"],
    todos: ["M3.4 7.4 5.6 9.6 9.4 5.8", "M12.4 7.7h8.2", "M3.4 15.9l2.2 2.2 3.8-3.8", "M12.4 16.2h8.2"],
    // A panel with its side rail, not a 2x2 grid of squares: four shapes with three gaps between
    // them turn to noise at the 13px this is drawn at in the workspace chip.
    workspace: ["M5.2 4.8h13.6a1.8 1.8 0 0 1 1.8 1.8v10.8a1.8 1.8 0 0 1-1.8 1.8H5.2a1.8 1.8 0 0 1-1.8-1.8V6.6a1.8 1.8 0 0 1 1.8-1.8Z",
                "M9.3 4.8V19.2"],
    settings: ["M9.65 5.51 10.14 2.89A9.3 9.3 0 0 1 13.86 2.89L14.35 5.51A6.9 6.9 0 0 1 14.93 5.75L17.13 4.24A9.3 9.3 0 0 1 19.76 6.87L18.25 9.07A6.9 6.9 0 0 1 18.49 9.65L21.11 10.14A9.3 9.3 0 0 1 21.11 13.86L18.49 14.35A6.9 6.9 0 0 1 18.25 14.93L19.76 17.13A9.3 9.3 0 0 1 17.13 19.76L14.93 18.25A6.9 6.9 0 0 1 14.35 18.49L13.86 21.11A9.3 9.3 0 0 1 10.14 21.11L9.65 18.49A6.9 6.9 0 0 1 9.07 18.25L6.87 19.76A9.3 9.3 0 0 1 4.24 17.13L5.75 14.93A6.9 6.9 0 0 1 5.51 14.35L2.89 13.86A9.3 9.3 0 0 1 2.89 10.14L5.51 9.65A6.9 6.9 0 0 1 5.75 9.07L4.24 6.87A9.3 9.3 0 0 1 6.87 4.24L9.07 5.75Z",
               "M12 15.1a3.1 3.1 0 1 1 0-6.2 3.1 3.1 0 0 1 0 6.2Z"],

    /* ---------- the agent ---------- */
    // Replaces 🤖 everywhere. A head, not a face: two eyes and an aerial, no mouth, so it reads
    // as a collaborator rather than a mascot.
    agent: ["M6.4 7.6h11.2a2.4 2.4 0 0 1 2.4 2.4v6.4a2.4 2.4 0 0 1-2.4 2.4H6.4A2.4 2.4 0 0 1 4 16.4V10a2.4 2.4 0 0 1 2.4-2.4Z",
            "M12 7.6V4.9",
            {d: "M12 2.2a1.5 1.5 0 1 1 0 3 1.5 1.5 0 0 1 0-3Z", fill: 1},
            {d: "M9.1 11.9a1.25 1.25 0 1 1 0 2.5 1.25 1.25 0 0 1 0-2.5Z", fill: 1},
            {d: "M14.9 11.9a1.25 1.25 0 1 1 0 2.5 1.25 1.25 0 0 1 0-2.5Z", fill: 1},
            "M2.2 12.2v2.2", "M21.8 12.2v2.2"],

    /* ---------- objects ---------- */
    users: ["M9.2 11.3a3.6 3.6 0 1 1 0-7.2 3.6 3.6 0 0 1 0 7.2Z",
            "M2.9 20.1v-1.4a4.4 4.4 0 0 1 4.4-4.4h3.8a4.4 4.4 0 0 1 4.4 4.4v1.4",
            "M16.6 4.5a3.6 3.6 0 0 1 0 6.5", "M17.5 14.4a4.4 4.4 0 0 1 3.6 4.3v1.4"],
    // Chalk talk: a board with a mark already on it. The mark is the point of the feature.
    talk: ["M7 3.4h13a1.5 1.5 0 0 1 1.5 1.5v9a1.5 1.5 0 0 1-1.5 1.5H7a1.5 1.5 0 0 1-1.5-1.5v-9A1.5 1.5 0 0 1 7 3.4Z",
           "M2.6 7.2v11.4A1.9 1.9 0 0 0 4.5 20.5h12.9",
           "M10 9.3c1 .8 1.8 1.7 2.5 2.8 1.6-2.6 3.2-4.6 5-6.2"],
    doc: ["M13.6 3.4H7.2a1.6 1.6 0 0 0-1.6 1.6v14a1.6 1.6 0 0 0 1.6 1.6h9.6a1.6 1.6 0 0 0 1.6-1.6V8.2Z",
          "M13.6 3.4v4.8h4.8", "M8.8 13h6.4", "M8.8 16.4h4.4"],
    folder: ["M3.4 7.6a1.6 1.6 0 0 1 1.6-1.6h3.9a1.2 1.2 0 0 1 .96.48l1.14 1.52h8.4a1.6 1.6 0 0 1 1.6 1.6v8.8a1.6 1.6 0 0 1-1.6 1.6H5a1.6 1.6 0 0 1-1.6-1.6Z"],
    clipboard: ["M9.2 4.4H7a1.6 1.6 0 0 0-1.6 1.6v13a1.6 1.6 0 0 0 1.6 1.6h10a1.6 1.6 0 0 0 1.6-1.6V6A1.6 1.6 0 0 0 17 4.4h-2.2",
                "M9.8 2.8h4.4a.9.9 0 0 1 .9.9v1.6a.9.9 0 0 1-.9.9H9.8a.9.9 0 0 1-.9-.9V3.7a.9.9 0 0 1 .9-.9Z",
                "M8.8 11.6h6.4", "M8.8 15.4h4.4"],
    archive: ["M3.6 4.6h16.8a.9.9 0 0 1 .9.9v2.6a.9.9 0 0 1-.9.9H3.6a.9.9 0 0 1-.9-.9V5.5a.9.9 0 0 1 .9-.9Z",
              "M5 9v9.4a1.6 1.6 0 0 0 1.6 1.6h10.8a1.6 1.6 0 0 0 1.6-1.6V9", "M10 13.1h4"],
    package: ["M12 3.2 20.4 7.6v8.8L12 20.8 3.6 16.4V7.6Z", "M3.6 7.6 12 12l8.4-4.4", "M12 12v8.8", "M7.8 5.4l8.4 4.4"],
    trash: ["M4.2 6.9h15.6", "M9.6 6.9V5.3a1.3 1.3 0 0 1 1.3-1.3h2.2a1.3 1.3 0 0 1 1.3 1.3v1.6",
            "M6.3 6.9l.86 12.1a1.5 1.5 0 0 0 1.5 1.4h6.68a1.5 1.5 0 0 0 1.5-1.4L17.7 6.9",
            "M10.2 10.6v6.1", "M13.8 10.6v6.1"],
    monitor: ["M4.6 4.6h14.8a1.6 1.6 0 0 1 1.6 1.6v8.6a1.6 1.6 0 0 1-1.6 1.6H4.6A1.6 1.6 0 0 1 3 14.8V6.2a1.6 1.6 0 0 1 1.6-1.6Z",
              "M12 16.4v3.9", "M8.2 20.3h7.6"],
    key: ["M14.6 3.6a5.8 5.8 0 1 1-5.35 8.05L3.4 17.5v3.1h3.1v-2.4h2.4v-2.4h2.4l1-1a5.8 5.8 0 0 1 2.3-11.2Z",
          {d: "M16.4 7.2a1.35 1.35 0 1 1 0 2.7 1.35 1.35 0 0 1 0-2.7Z", fill: 1}],
    link: ["M10.1 13.9a3.6 3.6 0 0 0 5.1 0l2.9-2.9a3.6 3.6 0 1 0-5.1-5.1L11.6 7.3",
           "M13.9 10.1a3.6 3.6 0 0 0-5.1 0l-2.9 2.9a3.6 3.6 0 1 0 5.1 5.1l1.4-1.4"],
    heart: ["M12 20.3S3.7 15.1 3.7 9.6a4.5 4.5 0 0 1 8.3-2.4 4.5 4.5 0 0 1 8.3 2.4c0 5.5-8.3 10.7-8.3 10.7Z"],
    command: ["M9 9h6v6H9Z", "M9 9H6.6a2.6 2.6 0 1 1 2.6-2.6V9", "M15 9h2.4a2.6 2.6 0 1 0-2.6-2.6V9",
              "M9 15H6.6a2.6 2.6 0 1 0 2.6 2.6V15", "M15 15h2.4a2.6 2.6 0 1 1-2.6 2.6V15"],
    search: ["M10.9 17.8a6.9 6.9 0 1 1 0-13.8 6.9 6.9 0 0 1 0 13.8Z", "M15.9 15.9l4.3 4.3"],
    warning: ["M10.55 4.4a1.65 1.65 0 0 1 2.9 0l7.2 13.3a1.65 1.65 0 0 1-1.45 2.4H4.8a1.65 1.65 0 0 1-1.45-2.4Z",
              "M12 9.6v4.4", {d: "M12 16.4a1.15 1.15 0 1 1 0 2.3 1.15 1.15 0 0 1 0-2.3Z", fill: 1}],
    info: ["M12 20.6a8.6 8.6 0 1 1 0-17.2 8.6 8.6 0 0 1 0 17.2Z", "M12 11.4v5",
           {d: "M12 6.9a1.15 1.15 0 1 1 0 2.3 1.15 1.15 0 0 1 0-2.3Z", fill: 1}],

    /* ---------- actions ---------- */
    close: ["M6.2 6.2 17.8 17.8", "M17.8 6.2 6.2 17.8"],
    check: ["M4.8 12.6 9.8 17.6 19.2 6.9"],
    plus: ["M12 5.2v13.6", "M5.2 12h13.6"],
    minus: ["M5.2 12h13.6"],
    pencil: ["M16.9 3.5a2.15 2.15 0 0 1 3.04 3.04L8.5 17.98 3.9 20.1l2.12-4.6Z", "M15.4 5l3.6 3.6"],
    copy: ["M9.4 9.4h9a1.6 1.6 0 0 1 1.6 1.6v9a1.6 1.6 0 0 1-1.6 1.6h-9A1.6 1.6 0 0 1 7.8 20v-9a1.6 1.6 0 0 1 1.6-1.6Z",
           "M4.6 14.6H4a1.6 1.6 0 0 1-1.6-1.6V4a1.6 1.6 0 0 1 1.6-1.6h9A1.6 1.6 0 0 1 14.6 4v.6"],
    refresh: ["M20.2 11.3A8.3 8.3 0 0 0 5.9 6.4L2.9 9.2", "M2.9 4.6v4.6h4.6",
              "M3.8 12.7a8.3 8.3 0 0 0 14.3 4.9l3-2.8", "M21.1 19.4v-4.6h-4.6"],
    undo: ["M9.3 5.6 4.2 10.7l5.1 5.1", "M4.2 10.7h9.9a5.6 5.6 0 0 1 0 11.2h-2.4"],
    redo: ["M14.7 5.6 19.8 10.7l-5.1 5.1", "M19.8 10.7H9.9a5.6 5.6 0 0 0 0 11.2h2.4"],
    // "landed in <page>" — the mark left the talk and became part of the document.
    "corner-down-right": ["M5 4.2v9.4a2.4 2.4 0 0 0 2.4 2.4H19", "M14.6 11.6 19.4 16l-4.8 4.4"],
    external: ["M14.2 3.8h6v6", "M20.2 3.8 11 13", "M18.2 14v5.4a1.6 1.6 0 0 1-1.6 1.6H4.8a1.6 1.6 0 0 1-1.6-1.6V7.6A1.6 1.6 0 0 1 4.8 6h5.4"],
    download: ["M12 3.6v11.2", "M7.4 10.4 12 15l4.6-4.6", "M3.8 18.2v1.2a1.6 1.6 0 0 0 1.6 1.6h13.2a1.6 1.6 0 0 0 1.6-1.6v-1.2"],
    upload: ["M12 15.2V4", "M7.4 8.4 12 3.8l4.6 4.6", "M3.8 18.2v1.2a1.6 1.6 0 0 0 1.6 1.6h13.2a1.6 1.6 0 0 0 1.6-1.6v-1.2"],
    expand: ["M3.9 9.2V5.5a1.6 1.6 0 0 1 1.6-1.6h3.7", "M14.8 3.9h3.7a1.6 1.6 0 0 1 1.6 1.6v3.7",
             "M20.1 14.8v3.7a1.6 1.6 0 0 1-1.6 1.6h-3.7", "M9.2 20.1H5.5a1.6 1.6 0 0 1-1.6-1.6v-3.7"],
    collapse: ["M9.2 3.9v3.7a1.6 1.6 0 0 1-1.6 1.6H3.9", "M14.8 3.9v3.7a1.6 1.6 0 0 0 1.6 1.6h3.7",
               "M20.1 14.8h-3.7a1.6 1.6 0 0 0-1.6 1.6v3.7", "M3.9 14.8h3.7a1.6 1.6 0 0 1 1.6 1.6v3.7"],
    "panel-left": ["M4.6 4.2h14.8a1.6 1.6 0 0 1 1.6 1.6v12.4a1.6 1.6 0 0 1-1.6 1.6H4.6A1.6 1.6 0 0 1 3 18.2V5.8a1.6 1.6 0 0 1 1.6-1.6Z",
                   "M9.6 4.2v15.6", {d: "M4.6 4.2h5v15.6h-5A1.6 1.6 0 0 1 3 18.2V5.8a1.6 1.6 0 0 1 1.6-1.6Z", fill: 1, faint: 1}],
    "panel-right": ["M4.6 4.2h14.8a1.6 1.6 0 0 1 1.6 1.6v12.4a1.6 1.6 0 0 1-1.6 1.6H4.6A1.6 1.6 0 0 1 3 18.2V5.8a1.6 1.6 0 0 1 1.6-1.6Z",
                    "M14.4 4.2v15.6", {d: "M14.4 4.2h5a1.6 1.6 0 0 1 1.6 1.6v12.4a1.6 1.6 0 0 1-1.6 1.6h-5Z", fill: 1, faint: 1}],
    "chevron-down": ["M6.2 9.4 12 15.2l5.8-5.8"],
    "chevron-up": ["M6.2 14.6 12 8.8l5.8 5.8"],
    "chevron-right": ["M9.4 6.2 15.2 12l-5.8 5.8"],
    "chevron-left": ["M14.6 6.2 8.8 12l5.8 5.8"],
    "arrow-right": ["M3.9 12h15.4", "M13.6 6.3 19.3 12l-5.7 5.7"],
    "arrow-left": ["M20.1 12H4.7", "M10.4 6.3 4.7 12l5.7 5.7"],
    more: [{d: "M12 3.9a1.55 1.55 0 1 1 0 3.1 1.55 1.55 0 0 1 0-3.1Z", fill: 1},
           {d: "M12 10.45a1.55 1.55 0 1 1 0 3.1 1.55 1.55 0 0 1 0-3.1Z", fill: 1},
           {d: "M12 17a1.55 1.55 0 1 1 0 3.1A1.55 1.55 0 0 1 12 17Z", fill: 1}],

    /* ---------- themes ---------- */
    "theme-dark": ["M20.4 14.6A8.9 8.9 0 0 1 9.4 3.6a8.9 8.9 0 1 0 11 11Z"],
    "theme-light": ["M12 16.6a4.6 4.6 0 1 1 0-9.2 4.6 4.6 0 0 1 0 9.2Z",
                    "M12 5.2V2.6", "M16.81 7.19 18.65 5.35", "M18.8 12h2.6", "M16.81 16.81l1.84 1.84",
                    "M12 18.8v2.6", "M7.19 16.81 5.35 18.65", "M5.2 12H2.6", "M7.19 7.19 5.35 5.35"],
    "theme-pink": ["M12 2.8 14.19 9.81 21.2 12l-7.01 2.19L12 21.2 9.81 14.19 2.8 12l7.01-2.19Z"],
    "theme-techno": ["M3.4 5.6h17.2a1.4 1.4 0 0 1 1.4 1.4v10a1.4 1.4 0 0 1-1.4 1.4H3.4A1.4 1.4 0 0 1 2 17V7a1.4 1.4 0 0 1 1.4-1.4Z",
                     "M6.4 9.6 9 12l-2.6 2.4", "M11.8 14.8h5.6"],
    "theme-pearl": ["M12 20.4a8.4 8.4 0 1 1 0-16.8 8.4 8.4 0 0 1 0 16.8Z",
                    "M7.5 14.4a5.2 5.2 0 0 1 6.9-6.9"],

    /* ---------- chalk-talk marks: drawn, not constructed ---------- */
    // Slight asymmetries are intentional. These sit on a slide the way a pen would.
    "mark-bad": ["M5.6 4.9c1.7 1.5 3.6 3.6 5.7 6.1 2.3 2.7 4.4 5.3 6.4 7.8",
                 "M18.7 5.1c-1.9 1.7-4.1 4-6.4 6.7-2.2 2.6-4.3 5.2-6.1 7.5"],
    "mark-q": ["M7.5 8c-.2-2.9 2-4.7 4.6-4.6 2.6.1 4.4 1.9 4.2 4.2-.2 2.6-2.9 3-3.8 4.6-.4.7-.5 1.5-.5 2.4",
               {d: "M11.9 17.7a1.55 1.55 0 1 1 0 3.1 1.55 1.55 0 0 1 0-3.1Z", fill: 1}],
    "mark-more": ["M2.9 13.1c3.6-.9 8-1.4 12-1.5 2.2 0 4.2.1 6.1.3",
                  "M14.6 5.9c1.3 2.1 3.1 4.1 5.6 5.9-2.4 1.7-4.3 3.7-5.7 6"],
    "mark-good": ["M3.5 12.6c1.4.7 2.6 1.8 3.8 3.4.6.8 1.1 1.6 1.6 2.4C11.5 13 15.1 8 20.2 3.9"],
    "mark-cut": ["M7.6 9.7a2.8 2.8 0 1 1 0-5.6 2.8 2.8 0 0 1 0 5.6Z",
                 "M7.6 19.9a2.8 2.8 0 1 1 0-5.6 2.8 2.8 0 0 1 0 5.6Z",
                 "M9.9 8.6c3.6 3.2 7.1 6.5 10.5 9.9", "M9.9 15.4C13.5 12.1 17 8.8 20.4 5.6"],
    /* `mark region` drags a box over a slide — a selection, not a pen. Drawn as a dashed frame
       with a drag handle so its silhouette is a rectangle, and cannot be confused with the two
       pens that sit beside it in the deck toolbar. */
    marquee: [{d: "M5.4 4.4h13.2a1.6 1.6 0 0 1 1.6 1.6v10.4a1.6 1.6 0 0 1-1.6 1.6H5.4a1.6 1.6 0 0 1-1.6-1.6V6a1.6 1.6 0 0 1 1.6-1.6Z", dash: "3.4 3"},
              {d: "M17.6 15.4a2.5 2.5 0 1 1 0 5 2.5 2.5 0 0 1 0-5Z", fill: 1}],
    "mark-ink": ["M2.6 18.4c1.2-3 2.8-5.1 4.3-5.7 2-.8 3.2.8 2.4 2.8-.7 1.8-2.6 2.6-3.6 1.4-1.2-1.5.3-4.4 2.6-6.5C11.1 7.6 15.5 5.6 21 4.6"],
    highlighter: ["M14.6 3.9 20.1 9.4 10.7 18.8H5.2v-5.5Z", "M12.2 6.3l5.5 5.5", "M3.2 21.4h17.6"],

    /* ---------- status ---------- */
    "check-circle": ["M12 20.6a8.6 8.6 0 1 1 0-17.2 8.6 8.6 0 0 1 0 17.2Z", "M8.1 12.1l2.7 2.7 5.3-5.7"],
    "dot-circle": ["M12 20.6a8.6 8.6 0 1 1 0-17.2 8.6 8.6 0 0 1 0 17.2Z",
                   {d: "M12 8.9a3.1 3.1 0 1 1 0 6.2 3.1 3.1 0 0 1 0-6.2Z", fill: 1}],
    eye: ["M2.2 12S5.9 5.4 12 5.4 21.8 12 21.8 12 18.1 18.6 12 18.6 2.2 12 2.2 12Z",
          "M12 15.2a3.2 3.2 0 1 1 0-6.4 3.2 3.2 0 0 1 0 6.4Z"],
    "eye-off": ["M9.9 5.7A9.4 9.4 0 0 1 12 5.4c6.1 0 9.8 6.6 9.8 6.6a17.8 17.8 0 0 1-3.2 4.1",
                "M6.4 7.5A17.4 17.4 0 0 0 2.2 12S5.9 18.6 12 18.6a9.6 9.6 0 0 0 4-.85",
                "M9.8 9.9a3.2 3.2 0 0 0 4.4 4.4", "M3.4 3.4l17.2 17.2"],
    clock: ["M12 20.6a8.6 8.6 0 1 1 0-17.2 8.6 8.6 0 0 1 0 17.2Z", "M12 7.1V12l3.3 2"]
  };

  var NS = "http://www.w3.org/2000/svg";
  var SPRITE_ID = "li-icon-sprite";

  function buildSprite() {
    var out = [];
    for (var name in P) {
      if (!Object.prototype.hasOwnProperty.call(P, name)) continue;
      var parts = P[name], body = "";
      for (var i = 0; i < parts.length; i++) {
        var p = parts[i];
        if (typeof p === "string") body += '<path d="' + p + '"/>';
        else if (p.dash) body += '<path d="' + p.d + '" stroke-dasharray="' + p.dash + '"/>';
        else body += '<path d="' + p.d + '" fill="currentColor" stroke="none"' +
                     (p.faint ? ' opacity=".38"' : "") + "/>";
      }
      out.push('<symbol id="li-i-' + name + '" viewBox="0 0 24 24">' + body + "</symbol>");
    }
    return out.join("");
  }

  function injectSprite() {
    if (document.getElementById(SPRITE_ID)) return;
    var host = document.createElementNS(NS, "svg");
    host.setAttribute("id", SPRITE_ID);
    host.setAttribute("aria-hidden", "true");
    // Not display:none — Safari has historically dropped <use> references into a display:none
    // sprite. Clipped to nothing instead, which every engine honours.
    host.setAttribute("style", "position:absolute;width:0;height:0;overflow:hidden");
    host.innerHTML = buildSprite();
    (document.body || document.documentElement).insertBefore(host, (document.body || document.documentElement).firstChild);
  }

  /** Build an <svg> element referencing one sprite symbol. */
  function LIIcon(name, opts) {
    opts = opts || {};
    var svg = document.createElementNS(NS, "svg");
    svg.setAttribute("class", "li-ic" + (opts.className ? " " + opts.className : ""));
    svg.setAttribute("viewBox", "0 0 24 24");
    svg.setAttribute("aria-hidden", "true");
    svg.setAttribute("focusable", "false");
    if (opts.size) { svg.style.width = opts.size + "px"; svg.style.height = opts.size + "px"; }
    var use = document.createElementNS(NS, "use");
    use.setAttribute("href", "#li-i-" + name);
    svg.appendChild(use);
    return svg;
  }

  /** Markup string, for the places that build HTML rather than nodes. */
  LIIcon.html = function (name, cls, size) {
    return '<svg class="li-ic' + (cls ? " " + cls : "") + '" viewBox="0 0 24 24" aria-hidden="true" focusable="false"' +
           (size ? ' style="width:' + size + "px;height:" + size + 'px"' : "") +
           '><use href="#li-i-' + name + '"/></svg>';
  };

  /** True when the name exists — lets callers fall back safely. */
  LIIcon.has = function (name) { return Object.prototype.hasOwnProperty.call(P, name); };
  LIIcon.names = function () { return Object.keys(P); };

  window.LIIcon = LIIcon;
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", injectSprite);
  else injectSprite();
})();
