/** Each shipped agent's product name (GLOSSARY: "agent" — Claude Code, Codex,
 * Aider…), for anywhere a provider id would otherwise be shown as words.
 *
 * The ids ("claude", "codex") stay the stored values and API keys; only what a
 * person reads changes. A custom agent (a user TOML) has no product name and
 * reads as its own id, capitalised.
 *
 * The server's agent-card sentences use the same names
 * (backend/web/addons/connections.py AGENT_NAMES). */
export const AGENT_NAMES: Record<string, string> = {
  claude: "Claude Code",
  codex: "Codex",
  aider: "Aider",
  antigravity: "Antigravity",
  opencode: "OpenCode",
  cline: "Cline",
  goose: "Goose",
};

export function agentName(provider: string): string {
  return AGENT_NAMES[provider] || (provider ? provider.charAt(0).toUpperCase() + provider.slice(1) : "Agent");
}
