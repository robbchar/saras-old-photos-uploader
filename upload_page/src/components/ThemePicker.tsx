// The theme (batch) picker on the "choose" screen. Purely presentational:
// App owns which theme is selected and reacts to onSelect - this component
// only renders the inline instruction, labels each option, and grays out the
// ones that have nothing ready to upload.

import { Select } from "radix-ui";
import type { ValidateBatch, ValidateCounts } from "../api/schemas";

export interface ThemePickerProps {
  batches: ValidateBatch[];
  value?: string;
  /** Disables the whole control - used while a run is uploading, when
   * switching themes would conflict with the one-run-at-a-time lock. */
  disabled?: boolean;
  onSelect: (value: string) => void;
}

/** Rows that still need a human before they can upload - invalid ones
 * (something is actively wrong) plus not-ready ones (still missing
 * fields) - summed across every state a row can be in. */
function countNeedingFixing(counts: ValidateCounts): number {
  return [counts.unassigned, counts.done, counts.reserved].reduce(
    (total, verdicts) => total + verdicts.invalid + verdicts.not_ready,
    0,
  );
}

function disabledReason(batch: ValidateBatch): string {
  const needFixing = countNeedingFixing(batch.counts);
  if (needFixing === 0) return "all uploaded";
  return `${needFixing} ${needFixing === 1 ? "needs" : "need"} fixing`;
}

function labelFor(batch: ValidateBatch): string {
  const detail =
    batch.ready_to_upload === 0 ? disabledReason(batch) : `${batch.ready_to_upload} ready`;
  return `${batch.value} — ${detail}`;
}

export function ThemePicker({ batches, value, disabled, onSelect }: ThemePickerProps) {
  return (
    <div className="flex flex-wrap items-center gap-3">
      <span id="theme-picker-label" className="text-text">
        Choose a theme to upload images for:
      </span>
      <Select.Root value={value} onValueChange={onSelect} disabled={disabled}>
        <Select.Trigger
          aria-labelledby="theme-picker-label"
          className="flex min-w-64 cursor-pointer items-center justify-between gap-2 rounded border border-border-strong bg-raised px-3 py-2 text-text disabled:cursor-not-allowed"
        >
          <Select.Value placeholder="Choose a theme" />
        </Select.Trigger>
        <Select.Portal>
          {/* position="popper" (not the default item-aligned) so the list
              still renders when every theme is disabled - item-aligned has no
              selectable item to anchor to then, and the popover comes up
              invisible (the "empty dropdown" bug). */}
          <Select.Content
            position="popper"
            sideOffset={4}
            className="min-w-[var(--radix-select-trigger-width)] overflow-hidden rounded border border-border bg-raised text-text shadow-none"
          >
            <Select.Viewport className="p-1">
              {batches.map((batch) => {
                const disabled = batch.ready_to_upload === 0;
                return (
                  <Select.Item
                    key={batch.value}
                    value={batch.value}
                    disabled={disabled}
                    className={
                      disabled
                        ? "cursor-default select-none rounded px-3 py-2 text-muted"
                        : "cursor-default select-none rounded px-3 py-2 text-text data-[highlighted]:bg-surface data-[highlighted]:outline-none"
                    }
                  >
                    <Select.ItemText>{labelFor(batch)}</Select.ItemText>
                  </Select.Item>
                );
              })}
            </Select.Viewport>
          </Select.Content>
        </Select.Portal>
      </Select.Root>
    </div>
  );
}
