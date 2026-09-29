// Auto/Light/Dark segmented control; presentational, useColorScheme owns the meaning.
// "Color scheme", not "theme": a theme here is an upload batch.

import { ToggleGroup } from "radix-ui";
import type { ReactNode } from "react";
import { isColorSchemePreference, type ColorSchemePreference } from "../colorScheme/colorScheme";

export interface ColorSchemeToggleProps {
  preference: ColorSchemePreference;
  onChange: (preference: ColorSchemePreference) => void;
}

interface SchemeOption {
  value: ColorSchemePreference;
  label: string;
  icon: ReactNode;
}

const iconProps = {
  width: 14,
  height: 14,
  viewBox: "0 0 24 24",
  fill: "none",
  stroke: "currentColor",
  strokeWidth: 2.2,
  strokeLinecap: "round",
  strokeLinejoin: "round",
  "aria-hidden": true,
} as const;

const OPTIONS: readonly SchemeOption[] = [
  {
    value: "system",
    label: "Auto (match this computer)",
    icon: (
      <svg {...iconProps}>
        <rect x="3" y="4" width="18" height="12" rx="1.5" />
        <path d="M8 20h8M12 16v4" />
      </svg>
    ),
  },
  {
    value: "light",
    label: "Light",
    icon: (
      <svg {...iconProps}>
        <circle cx="12" cy="12" r="4" />
        <path d="M12 2v2M12 20v2M2 12h2M20 12h2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4" />
      </svg>
    ),
  },
  {
    value: "dark",
    label: "Dark",
    icon: (
      <svg {...iconProps}>
        <path d="M20.5 14.5A8.5 8.5 0 0 1 9.5 3.5a8.5 8.5 0 1 0 11 11z" />
      </svg>
    ),
  },
];

export function ColorSchemeToggle({ preference, onChange }: ColorSchemeToggleProps) {
  return (
    <ToggleGroup.Root
      type="single"
      value={preference}
      // Radix reports "" when the checked item is clicked again; keep the current choice.
      onValueChange={(value) => {
        if (isColorSchemePreference(value) && value !== preference) onChange(value);
      }}
      aria-label="Color scheme"
      className="inline-flex rounded border border-border-strong bg-surface p-0.5"
    >
      {OPTIONS.map((option) => (
        <ToggleGroup.Item
          key={option.value}
          value={option.value}
          aria-label={option.label}
          title={option.label}
          className="grid h-7 w-8 cursor-pointer place-items-center rounded-sm text-muted hover:text-text data-[state=on]:bg-accent data-[state=on]:text-on-accent"
        >
          {option.icon}
        </ToggleGroup.Item>
      ))}
    </ToggleGroup.Root>
  );
}
