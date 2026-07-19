/**
 * Global branding & appearance (Control Plane > Preferences), delivered by the
 * public GET /api/branding endpoint so it applies even before login.
 *
 * One context carries: the admin-set logos (per surface), the UI/AI language,
 * and — as side effects on <html> — the font-size and chat-background choices.
 * A minimal, dependency-free i18n (`useT`) reads the language from here; it
 * covers the KEY user-facing surfaces (nav / auth / chat) only, by design —
 * deep admin/settings forms stay English.
 */
import { createContext, useCallback, useContext, useEffect, useState } from "react";
import {
  api,
  type BrandingChatBackground,
  type BrandingFontSize,
  type BrandingLanguage,
  type BrandingLogoSlot,
  type PublicBranding,
} from "./api";

const DEFAULT_BRANDING: PublicBranding = {
  language: "en",
  font_size: "small",
  chat_background: "solid",
  logos: { login: null, chat: null, sidebar: null },
};

interface BrandingContextValue {
  branding: PublicBranding;
  /** Re-fetch after an admin edit so the change is visible without a reload. */
  refresh: () => Promise<void>;
}

const BrandingContext = createContext<BrandingContextValue>({
  branding: DEFAULT_BRANDING,
  refresh: async () => {},
});

/** Apply the appearance choices that live as root-level attributes (the CSS in
 * styles.css keys off these). Kept out of React render so it also works for the
 * standalone share view. */
function applyAppearance(font_size: BrandingFontSize, chat_background: BrandingChatBackground) {
  const root = document.documentElement;
  root.setAttribute("data-font-size", font_size);
  root.setAttribute("data-chat-bg", chat_background);
}

export function BrandingProvider({ children }: { children: React.ReactNode }) {
  const [branding, setBranding] = useState<PublicBranding>(DEFAULT_BRANDING);

  const load = useCallback(async () => {
    try {
      const cfg = await api.getBranding();
      setBranding(cfg);
      applyAppearance(cfg.font_size, cfg.chat_background);
    } catch {
      // Network/endpoint failure → keep the built-in defaults (bundled logo,
      // English, small, solid). Branding is cosmetic; never block the app.
      applyAppearance(DEFAULT_BRANDING.font_size, DEFAULT_BRANDING.chat_background);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  return (
    <BrandingContext.Provider value={{ branding, refresh: load }}>{children}</BrandingContext.Provider>
  );
}

export function useBranding(): BrandingContextValue {
  return useContext(BrandingContext);
}

/** Resolve the URL for a logo slot: the admin-uploaded asset if set, else the
 * bundled default. */
export function useLogoSrc(slot: BrandingLogoSlot, fallback: string): string {
  const { branding } = useBranding();
  return branding.logos[slot] || fallback;
}

// ---- minimal i18n (key surfaces only) --------------------------------------

type Dict = Record<string, string>;

const TRANSLATIONS: Record<BrandingLanguage, Dict> = {
  en: {
    "nav.newChat": "New chat",
    "nav.searchChats": "Search chats...",
    "nav.settings": "Settings",
    "nav.controlPlane": "Control Plane",
    "nav.logout": "Log out",
    "auth.tagline": "Your personal AI agent",
    "chat.composerPlaceholder": "Message Claw...",
    "chat.composerEmpty": "How can Claw help you today?",
  },
  th: {
    "nav.newChat": "แชทใหม่",
    "nav.searchChats": "ค้นหาแชท...",
    "nav.settings": "ตั้งค่า",
    "nav.controlPlane": "แผงควบคุม",
    "nav.logout": "ออกจากระบบ",
    "auth.tagline": "ผู้ช่วย AI ส่วนตัวของคุณ",
    "chat.composerPlaceholder": "พิมพ์ข้อความถึง Claw...",
    "chat.composerEmpty": "วันนี้ให้ Claw ช่วยอะไรดีครับ?",
  },
};

/** Returns a translator bound to the current global language. Unknown keys fall
 * back to the English string, then to the key itself, so a missing translation
 * degrades visibly-but-safely rather than crashing. */
export function useT(): (key: string) => string {
  const { branding } = useBranding();
  const lang = branding.language;
  return useCallback(
    (key: string) => TRANSLATIONS[lang]?.[key] ?? TRANSLATIONS.en[key] ?? key,
    [lang],
  );
}
