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

/** Rows that are neither ready to upload nor already uploaded - invalid ones
 * (something is actively wrong) plus not-ready ones (still missing fields) -
 * summed across every state a row can be in. The dropdown reports these with a
 * generic "not ready"; the Preview breaks the same rows into "Needs fixing"
 * (invalid) and "Not yet catalogued" (missing fields), so the dropdown must NOT
 * reuse either of those specific words - "N need fixing" up top while the
 * Preview says "Needs fixing: None" reads as a contradiction. */
function countNotReady(counts: ValidateCounts): number {
  return [counts.unassigned, counts.done, counts.reserved].reduce(
    (total, verdicts) => total + verdicts.invalid + verdicts.not_ready,
    0,
  );
}

function unreadyReason(batch: ValidateBatch): string {
  const notReady = countNotReady(batch.counts);
  if (notReady === 0) return "all uploaded";
  return `${notReady} not ready`;
}

function labelFor(batch: ValidateBatch): string {
  const detail =
    batch.ready_to_upload === 0 ? unreadyReason(batch) : `${batch.ready_to_upload} ready`;
  return `${batch.value} — ${detail}`;
}

export function ThemePicker({ batches, value, disabled, loading, onSelect }: ThemePickerProps) {
  // Render the selected theme's label straight from `batches` rather than
  // relying on Radix's <Select.Value>, which snapshots the item's text at
  // selection time and does not refresh when a re-check updates that theme's
  // counts -- the trigger would keep showing the stale "N not ready".
  const selectedBatch = value === undefined ? undefined : batches.find((batch) => batch.value === value);
  return (
    <div className="flex flex-wrap items-center gap-3">
      <span id="theme-picker-label" className="text-text">
        Choose a theme to upload items for:
      </span>
      <Select.Root value={value} onValueChange={onSelect} disabled={disabled}>
        <Select.Trigger
          aria-labelledby="theme-picker-label"
          aria-busy={loading}
          className="flex min-w-64 cursor-pointer items-center justify-between gap-2 rounded border border-border-strong bg-raised px-3 py-2 text-text disabled:cursor-not-allowed"
        >
          {loading
            ? "Loading themes…"
            : selectedBatch
              ? labelFor(selectedBatch)
              : <Select.Value placeholder="Choose a theme" />}
        </Select.Trigger>
        <Select.Portal>
          {/* position="popper" (not the default item-aligned) so the list
              still renders when every theme is disabled - item-aligned has no
              selectable item to anchor to then, and the popover comes up
              invisible (the "empty dropdown" bug). */}
          <Select.Content
            position="popper"
            sideOffset={4}
            className="min-w-[var(--radix-select-trigger-width)] overflow-hidden rounded border border-border bg-raised text-text shadow-none outline-none"
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
