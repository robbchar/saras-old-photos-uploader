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
  /** True while the theme list is still being fetched: the trigger shows a
   * loading label in place of the placeholder and is marked aria-busy, so it
   * is clear WHY the control cannot be used yet. */
  loading?: boolean;
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

export function ThemePicker({ batches, value, disabled, loading, onSelect }: ThemePickerProps) {
  return (
    <div className="flex flex-wrap items-center gap-3">
      <span id="theme-picker-label" className="text-text">
        Choose a theme to upload images for:
      </span>
      <Select.Root value={value} onValueChange={onSelect} disabled={disabled}>
        <Select.Trigger
          aria-labelledby="theme-picker-label"
          aria-busy={loading}
          className="flex min-w-64 cursor-pointer items-center justify-between gap-2 rounded border border-border-strong bg-raised px-3 py-2 text-text disabled:cursor-not-allowed"
        >
          {loading ? "Loading themes…" : <Select.Value placeholder="Choose a theme" />}
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
                // Every theme is selectable, even with nothing ready: you pick
                // it to see its detail (what's uploaded, what needs fixing). A
                // zero-ready theme is only de-emphasized, never disabled.
                const nothingReady = batch.ready_to_upload === 0;
                return (
                  <Select.Item
                    key={batch.value}
                    value={batch.value}
                    className={`cursor-pointer select-none rounded px-3 py-2 data-[highlighted]:bg-surface data-[highlighted]:outline-none ${
                      nothingReady ? "text-muted" : "text-text"
                    }`}
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
