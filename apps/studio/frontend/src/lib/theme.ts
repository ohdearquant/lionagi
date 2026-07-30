export type StudioTheme = "dark" | "light";

export const THEME_CHANGE_EVENT = "studio:theme-change";

export function getTheme(): StudioTheme {
  if (typeof document === "undefined") return "dark";
  return document.documentElement.getAttribute("data-theme") === "light" ? "light" : "dark";
}

export function applyTheme(theme: StudioTheme): void {
  if (typeof document === "undefined") return;
  document.documentElement.setAttribute("data-theme", theme);
  document.documentElement.classList.toggle("dark", theme === "dark");
  window.localStorage.setItem("theme", theme);
  window.dispatchEvent(new CustomEvent<StudioTheme>(THEME_CHANGE_EVENT, { detail: theme }));
}
