# Are.na Archive Design System

## Direction

The archive uses a quiet editorial interface: warm paper, black type, thin rules,
small monospace labels, and dense image-led content. It should feel like a
personal working archive rather than a dashboard.

The visual language is intentionally restrained. Hierarchy comes from scale,
spacing, position, and a small set of accent colors instead of decoration.

## Color

```text
Paper       #f5f5f0   Main canvas and modal surface
Ink         #111111   Primary text and rules
Muted       #777777   Metadata, labels, and secondary text
Line        #d8d8d3   Hairline borders and grid dividers
Accent      #e6ff3f   Hover and action highlight
Channel     #b9d8ff   Nested channel cards
Channel ink #12345b   Nested channel text
Danger      #b91919   Destructive hover state
```

Use `Paper` as the default background and `Ink` for structural contrast. Use
`Accent` sparingly for hover states and action affordances. Blue is reserved
for channel blocks nested inside another channel.

## Typography

- Display: system sans-serif, large, tight, and negative in tracking
- Interface: Arial/Helvetica system sans-serif
- Machinery: monospace for labels, tabs, metadata, and controls
- Long titles: allow overflow wrapping and add break opportunities after `_`

Labels are uppercase with wide tracking. Titles are sentence case and visually
heavy. Body text is muted and compact.

## Layout

- The top bar is short, sticky, and structural.
- The main page starts with a large archive heading.
- `VIEW` sits above `EDITING` as a pair of horizontal control bands.
- Channel cards use a responsive grid with one-pixel rules between cells.
- Block pages use a CSS masonry layout with three columns on wide screens.
- The layout collapses to two masonry columns on small screens.
- Keep generous outer margins and avoid unnecessary panels or shadows.

## Navigation

The main page is the canonical channel view. View controls change the main page
rather than opening a second nested browsing surface.

- `ALL` shows every channel.
- `ABC` sorts alphabetically and toggles ascending/descending order.
- `NEWEST` sorts by channel update time.
- Category names filter the main page directly.
- The live search filters visible channel cards as the user types.

## Components

### Channel Card

Channel cards show the category, block count, title, and description. The whole
card navigates to the channel. The category chip is a separate filter action.

### Block Card

Block cards preserve the archive order. Visual blocks lead with media; text,
links, attachments, and embeds use quieter fallback surfaces. The remove action
appears on hover and remains available through keyboard focus.

### Nested Channel Card

Nested channels use the blue channel treatment and link directly to the nested
channel. Their contents are not flattened into the parent channel.

### Post Modal

Clicking a block opens a modal with the complete local block content. The modal
closes through the close control, backdrop, or `Escape`. Source links inside the
modal open in a new tab.

### Editing

Local editing is grouped in one `EDITING` area. It supports creating channels,
creating categories, adding text/link blocks, removing blocks from channels, and
deleting channels. These operations affect the local archive only.

### Selection

Blocks on a channel page and channel cards on the main page can be selected
together. `SELECT BLOCKS` / `SELECT CHANNELS` starts selecting. ⌘-click also
starts it, and shift-click selects a range. Selected items get an ink outline
and an accent check. A black bar pinned to the bottom of the window shows the
count and the actions:

- `RENAME` gives every item one name, numbered in order when more than one is
  selected. `#` in the name marks where the number goes.
- `MOVE TO…` picks an existing channel, or makes a new one from the typed
  name. Blocks can leave this channel or stay in it too, and a new channel can
  sit inside this one where the first moved block was. Channels are nested
  inside the chosen channel and stay on the main page.
- `MERGE` (channels only) puts every block of the selected channels into one
  new, named channel, in order and without duplicates. The new channel takes
  the originals' place wherever they were nested. The originals are deleted
  unless kept.
- `DELETE` asks first, with focus on `CANCEL`. A block that another channel
  still holds is only taken out of this one.

`⌘A` selects everything visible, `Delete` asks to delete the selection, and
`Escape` stops selecting. Actions run in a small paper dialog, never in
`prompt()`, because the Mac app's web view does not show one.

## Interaction Rules

- Prefer direct navigation over hidden state.
- Use hover for secondary actions, but preserve keyboard focus behavior.
- Do not make destructive actions visually dominant.
- Keep source links separate from modal-opening behavior.
- Never silently omit content during an import; record import failures.
- Keep private archive data local and serve it only from localhost.

## Responsive Behavior

At narrow widths:

- Reduce outer page padding.
- Keep the top bar compact.
- Reduce the block masonry to two columns.
- Allow controls to wrap naturally.
- Preserve readable title wrapping with `overflow-wrap: anywhere`.

## Data and Content Rules

- Imported blocks are globally deduplicated by Are.na ID.
- Channel membership preserves ordering and connection metadata.
- Nested channel contents are intentionally not traversed by the importer.
- Local records use negative IDs so imported IDs remain untouched.
- Downloaded assets are served with their stored MIME type.
