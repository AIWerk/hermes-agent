export interface CuiProfileAuthority {
  active_profile?: string | null;
  default_profile?: string | null;
}

function clean(value?: string | null): string | undefined {
  const normalized = value?.trim();
  return normalized || undefined;
}

export function activeProfileFromAuth(authority?: CuiProfileAuthority | null): string | undefined {
  return clean(authority?.active_profile) ?? clean(authority?.default_profile);
}

export function assistantDocumentTitle(agentName?: string | null): string {
  const name = clean(agentName);
  return name ? `${name} AI Assistant` : "AI Assistant";
}