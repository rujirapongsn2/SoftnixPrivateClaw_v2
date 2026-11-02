import { Icon, type IconName, type IconType } from "@astryxdesign/core/Icon";
import { useT } from "./branding";

export type PageNavItem<K extends string, G extends string> = {
  key: K;
  group: G;
  labelKey: string;
  icon: IconType | IconName;
};

// In-page section nav shared by Settings and the Control Plane: items listed
// under group titles in `groups` order, empty groups left out.
export function GroupedPageNav<K extends string, G extends string>({
  label,
  groups,
  groupLabelPrefix,
  items,
  selected,
  onSelect,
}: {
  label: string;
  groups: readonly G[];
  groupLabelPrefix: string;
  items: readonly PageNavItem<K, G>[];
  selected: K;
  onSelect: (key: K) => void;
}) {
  const t = useT();
  return (
    <nav className="claw-cp-nav" aria-label={label}>
      {groups.map((group) => {
        const inGroup = items.filter((item) => item.group === group);
        if (inGroup.length === 0) return null;
        return (
          <div className="claw-cp-group" key={group}>
            <div className="claw-cp-group-title">{t(`${groupLabelPrefix}${group}`)}</div>
            {inGroup.map((item) => (
              <button key={item.key} type="button"
                className={selected === item.key ? "is-selected" : undefined}
                aria-current={selected === item.key ? "page" : undefined}
                onClick={() => onSelect(item.key)}>
                <Icon icon={item.icon} size="sm" />
                <span>{t(item.labelKey)}</span>
              </button>
            ))}
          </div>
        );
      })}
    </nav>
  );
}
