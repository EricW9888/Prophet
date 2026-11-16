# Workspace presentation

Prophet is a full-width personal workspace. Put the current decision context,
material uncertainty and next useful action before secondary history and system
records. Preserve clear distinctions between recorded values, estimates,
unavailable values and incomplete evidence.

Unqualified Review navigation starts with investment concerns. All reviews and
portfolio-data reviews remain explicit counted views; portfolio data warnings
stay visible on Portfolio. A manual challenge requires a selected or linked
subject rather than silently choosing the first research profile.

Use existing semantic surface, text and action tokens. Shared small body text
uses 15px at the default browser scale; supporting text uses 13px. Use the shared
`text-sm` and `text-xs` tokens instead of local 9px/10px/11px caption sizes. Check
layout at compact and wide widths instead of making the workspace artificially narrow.
Charts and dense tables retain their own layout and interaction requirements.

Use native disclosure controls for secondary material. `DeferredDisclosure`
mounts children on first opening and preserves them after closing, so hidden
panels do not issue their own reads before use and closing does not discard a
draft or selection. A collapsed panel must not conceal a material warning or the
only path to a required action. Ordinary loading and error states stay visible.

`RecordFields` labels properties for reading, preserves literal scalar values
and multiline text, and expands nested records on demand. Date elements retain
their original timestamp. The exact raw record remains available separately;
construct it only when requested. A presentation change must not rewrite stored
evidence or reinterpret an identifier as a business label.

Keep citations beside the claims or proposals they support; offer the remaining
captured packet and configuration on demand. A disclosure is not a substitute
for a missing source, unresolved calculation or incomplete workflow.

References: [NN/g progressive disclosure](https://www.nngroup.com/articles/progressive-disclosure/)
informs the primary/secondary split. [W3C contrast guidance](https://www.w3.org/WAI/WCAG22/Understanding/contrast-minimum.html)
defines the 4.5:1 minimum used by the semantic-text-token checks. Those checks do
not establish that every bespoke chart, color pair or interaction conforms.
