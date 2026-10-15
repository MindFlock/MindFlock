/** Which agent runs my half of a shared folder: the installed agents that can
 * run in the sandbox here, the default first (the server orders them so). */

export function AgentSelect(props: { agents: string[]; value: string; onChange(v: string): void; className?: string }) {
  const { agents, value, onChange, className } = props;
  return (
    <select className={className} value={value || agents[0] || ""} onChange={(e) => onChange(e.target.value)} disabled={!agents.length} aria-label="Agent">
      {agents.length ? (
        agents.map((a) => (
          <option value={a} key={a}>
            {a}
          </option>
        ))
      ) : (
        <option value="">no agent can run here</option>
      )}
    </select>
  );
}
