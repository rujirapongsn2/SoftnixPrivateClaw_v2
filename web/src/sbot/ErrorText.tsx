import { Icon } from "@astryxdesign/core/Icon";
import { Text } from "@astryxdesign/core/Text";

export function ErrorText({ children }: { children: React.ReactNode }) {
  return (
    <div className="claw-row claw-error">
      <Icon icon="warning" color="warning" size="sm" />
      <Text color="inherit" size="sm">
        {children}
      </Text>
    </div>
  );
}
