.pragma library

// Escaping shared by highlight.js (search snippets) and chat.js (answers):
// note and model text is user text, escaped before any StyledText markup
// is added, so "<b>" or "&" in it shows literally.

function escapeHtml(s) {
  return String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;")
}

// A colour that is safe inside a font tag's attribute, or the default accent.
function safeColor(color) {
  return /^#[0-9a-fA-F]{6}([0-9a-fA-F]{2})?$/.test(String(color)) ? String(color) : "#e0a030"
}
