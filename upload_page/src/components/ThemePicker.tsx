// The theme (batch) picker on the "choose" screen. Purely presentational:
// App owns which theme is selected and reacts to onSelect - this
// component only knows how to label and gray out an option.

import { Select } from "radix-ui";
import type { ValidateBatch, ValidateCounts } from "../api/schemas";

export interface ThemePickerProps {
  batches: ValidateBatch[];
  value?: string;
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
  return needFixing > 0 ? `${needFixing} need fixing` : "all uploaded";
}

function labelFor(batch: ValidateBatch): string {
  const detail =
    batch.ready_to_upload === 0 ? disabledReason(batch) : `${batch.ready_to_upload} ready`;
  return `${batch.value} \u2014 ${detail}`;
}

export function ThemePicker({ batches, value, onSelect }: ThemePickerProps) {
  return (
    <Select.Root value={value} onValueChange={onSelect}>
      <Select.Trigger
        aria-label="Choose a theme"
        className="flex min-w-64 items-center justify-between gap-2 rounded border border-border-strong bg-raised px-3 py-2 text-text"
      >
        <Select.Value placeholder="Choose a theme" />
      </Select.Trigger>
      <Select.Portal>
        <Select.Content className="overflow-hidden rounded border border-border bg-raised text-text shadow-none">
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
                      ? "cursor-default select-none rounded px-3 py-2 text-faint"
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
  );
}
