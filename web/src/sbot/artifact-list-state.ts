export type ArtifactExpansion = { key: string; expanded: boolean };

export function isArtifactListExpanded(state: ArtifactExpansion, key: string): boolean {
  return state.key === key && state.expanded;
}

export function toggleArtifactList(state: ArtifactExpansion, key: string): ArtifactExpansion {
  return {
    key,
    expanded: state.key === key ? !state.expanded : true,
  };
}
