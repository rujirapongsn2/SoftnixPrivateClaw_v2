import { TextInput } from "@astryxdesign/core/TextInput";
import { useState } from "react";

// Letters (minus visually-confusable 0/O, 1/l/I) + digits + a few shell/URL-safe
// symbols — long and varied enough to be strong, easy enough to read back if
// the admin needs to relay it manually.
const PASSWORD_CHARSET =
  "ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz23456789!@#$%^&*-_=+";

export function generatePassword(length = 16): string {
  const bytes = new Uint32Array(length);
  crypto.getRandomValues(bytes);
  return Array.from(bytes, (b) => PASSWORD_CHARSET[b % PASSWORD_CHARSET.length]).join("");
}

/**
 * A password TextInput with a one-click strong-password generator and a
 * show/hide toggle — generating a value the admin can't read back would defeat
 * the point, so revealing it is automatic on generate.
 */
export function PasswordField({
  label,
  value,
  onChange,
  description,
  placeholder,
}: {
  label: string;
  value: string;
  onChange: (v: string) => void;
  description?: string;
  placeholder?: string;
}) {
  const [visible, setVisible] = useState(false);

  return (
    <div className="claw-password-field">
      <TextInput
        label={label}
        type={visible ? "text" : "password"}
        description={description}
        placeholder={placeholder}
        value={value}
        onChange={onChange}
      />
      <div className="claw-password-actions">
        <button
          type="button"
          className="claw-link-btn"
          onClick={() => {
            onChange(generatePassword());
            setVisible(true);
          }}
        >
          Generate password
        </button>
        {value && (
          <button type="button" className="claw-link-btn" onClick={() => setVisible((v) => !v)}>
            {visible ? "Hide" : "Show"}
          </button>
        )}
      </div>
    </div>
  );
}
