/* Re-render diagrams after Material's instant navigation swaps the page. */
document$.subscribe(() => {
  if (typeof mermaid === "undefined") return;

  mermaid.initialize({
    startOnLoad: false,
    securityLevel: "loose",
    theme: document.body.getAttribute("data-md-color-scheme") === "slate"
      ? "dark"
      : "neutral",
    fontFamily: "var(--lion-font-ui)",
  });

  mermaid.run({ querySelector: ".mermaid" });
});
