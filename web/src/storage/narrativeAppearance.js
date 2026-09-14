export const shortBookTitle = (title) => {
  const chars = Array.from(String(title || ""));
  return chars.length > 16 ? `${chars.slice(0, 16).join("")}…` : chars.join("");
};

export const chronologicalSources = (sources = []) => [...sources].sort((a, b) => {
  const time = (source) => {
    const value = Date.parse(source.date || "");
    return Number.isNaN(value) ? Infinity : value;
  };
  return (time(a) - time(b)) || String(a.id || a.source_id).localeCompare(String(b.id || b.source_id));
});

// Paper and mist families vary quietly across the shelf; deep green stays 16+.
export const bookAppearance = (count = 0, position = 0) => {
  const sources = Math.max(0, Number(count) || 0);
  const dark = sources > 15;
  const palette = sources > 5 ? ["#dbe2df", "#e4dfd2", "#ccd9d4"] : ["#fdfdfb", "#f5f0e6", "#eaf0ee"];
  const variant = Math.abs(Math.trunc(Number(position) || 0)) % palette.length;
  return {
    "--book-cover": dark ? "#596660" : palette[variant],
    "--book-ink": dark ? "#f7faf8" : "#293832",
    "--book-muted": dark ? "rgba(247,250,248,.78)" : "#54645d",
  };
};
