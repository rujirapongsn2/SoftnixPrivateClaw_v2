import type { BotInfo } from "./api";

type Translate = (key: string, params?: Record<string, string>) => string;

const SYSTEM_TEAM_LEAD_NAMES = new Set(["บุ้ย", "Default", "Team Lead", "หัวหน้าทีม"]);

/** Localize the factory Chief of Staff name without masking a name the owner chose. */
export function botDisplayName(bot: BotInfo, t: Translate): string {
  if (
    bot.kind === "chief_of_staff"
    && bot.created_by === "system"
    && SYSTEM_TEAM_LEAD_NAMES.has(bot.name)
  ) {
    return t("bot.teamLeadName");
  }
  return bot.name;
}
