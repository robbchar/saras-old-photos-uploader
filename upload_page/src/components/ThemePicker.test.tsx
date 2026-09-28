import { afterEach, beforeAll, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import type { ValidateBatch } from "../api/schemas";
import { ThemePicker } from "./ThemePicker";

afterEach(cleanup);

// jsdom has no layout engine, so it never implemented scrollIntoView.
// Radix's Select scrolls the selected/first item into view as soon as its
// listbox opens (see @radix-ui/react-select's item-aligned positioning),
// which would otherwise throw "scrollIntoView is not a function" the
// moment a test opens the picker.
beforeAll(() => {
  Element.prototype.scrollIntoView = vi.fn();
});

const ZERO_VERDICTS = { ready: 0, invalid: 0, not_ready: 0 };
const ZERO_COUNTS = { unassigned: ZERO_VERDICTS, done: ZERO_VERDICTS, reserved: ZERO_VERDICTS };

const READY_BATCH: ValidateBatch = {
  value: "Fishing",
  ready_to_upload: 1,
  counts: {
    ...ZERO_COUNTS,
    unassigned: { ready: 1, invalid: 0, not_ready: 2 },
  },
};

// Nothing left ready, but rows still need attention - "N not ready". The
// dropdown stays generic; the Preview says whether they need fixing (invalid)
// or cataloguing (missing fields).
const NOT_READY_BATCH: ValidateBatch = {
  value: "Waterfront",
  ready_to_upload: 0,
  counts: {
    ...ZERO_COUNTS,
    unassigned: { ready: 0, invalid: 2, not_ready: 1 },
  },
};

// Exactly one not-ready row - "1 not ready".
const ONE_NOT_READY_BATCH: ValidateBatch = {
  value: "Harborside",
  ready_to_upload: 0,
  counts: {
    ...ZERO_COUNTS,
    unassigned: { ready: 0, invalid: 0, not_ready: 1 },
  },
};

// Nothing left ready, and nothing broken either - everything already
// uploaded (done rows carry no invalid/not_ready of their own here).
const ALL_UPLOADED_BATCH: ValidateBatch = {
  value: "Harbor",
  ready_to_upload: 0,
  counts: {
    ...ZERO_COUNTS,
    done: { ready: 0, invalid: 0, not_ready: 0 },
  },
};

// Opens the (closed) listbox by clicking the trigger. Radix's
// SelectTrigger only opens on click when the last pointer interaction
// wasn't a mouse pointerdown - true here, since fireEvent.click never
// dispatches one - so this matches how a keyboard/touch user opens it
// without needing to polyfill PointerEvent/hasPointerCapture.
function openPicker() {
  fireEvent.click(screen.getByRole("combobox", { name: /choose a theme/i }));
}

describe("ThemePicker", () => {
  it("shows an enabled theme's ready count and selects it", () => {
    const onSelect = vi.fn();
    render(
      <ThemePicker batches={[READY_BATCH, NOT_READY_BATCH]} onSelect={onSelect} />,
    );

    openPicker();
    const option = screen.getByRole("option", { name: "Fishing — 1 ready" });
    expect(option).not.toHaveAttribute("aria-disabled", "true");

    fireEvent.click(option);
    expect(onSelect).toHaveBeenCalledWith("Fishing");
  });

  it("still lets you select a zero-ready theme with rows needing attention, labeled 'N not ready'", () => {
    const onSelect = vi.fn();
    render(<ThemePicker batches={[NOT_READY_BATCH]} onSelect={onSelect} />);

    openPicker();
    // Generic "not ready" up top, never "N need fixing" -- the Preview alone
    // says which are invalid vs not-yet-catalogued.
    const option = screen.getByRole("option", { name: "Waterfront — 3 not ready" });
    expect(option).not.toHaveAttribute("aria-disabled", "true");
    fireEvent.click(option);
    expect(onSelect).toHaveBeenCalledWith("Waterfront");
  });

  it("labels a single not-ready row as '1 not ready'", () => {
    render(<ThemePicker batches={[ONE_NOT_READY_BATCH]} onSelect={vi.fn()} />);

    openPicker();
    expect(screen.getByRole("option", { name: "Harborside — 1 not ready" })).toBeInTheDocument();
  });

  it("still lets you select an all-uploaded theme, labeled 'all uploaded'", () => {
    const onSelect = vi.fn();
    render(<ThemePicker batches={[ALL_UPLOADED_BATCH]} onSelect={onSelect} />);

    openPicker();
    const option = screen.getByRole("option", { name: "Harbor — all uploaded" });
    expect(option).not.toHaveAttribute("aria-disabled", "true");
    fireEvent.click(option);
    expect(onSelect).toHaveBeenCalledWith("Harbor");
  });

  it("shows the instruction inline and uses it as the picker's accessible name", () => {
    render(<ThemePicker batches={[READY_BATCH]} onSelect={vi.fn()} />);
    expect(screen.getByText("Choose a theme to upload items for:")).toBeInTheDocument();
    expect(
      screen.getByRole("combobox", { name: /choose a theme to upload items for/i }),
    ).toBeInTheDocument();
  });

  it("renders zero-ready themes de-emphasized with a legible token, not the near-invisible faint one", () => {
    render(<ThemePicker batches={[NOT_READY_BATCH]} onSelect={vi.fn()} />);
    openPicker();
    const option = screen.getByRole("option", { name: "Waterfront — 3 not ready" });
    expect(option.className).toContain("text-muted");
    expect(option.className).not.toContain("text-faint");
  });

  it("disables the whole control when `disabled` is set", () => {
    render(<ThemePicker batches={[READY_BATCH]} disabled onSelect={vi.fn()} />);
    expect(screen.getByRole("combobox", { name: /choose a theme/i })).toBeDisabled();
  });

  it("shows a loading label while themes are still being fetched", () => {
    render(<ThemePicker batches={[]} loading disabled onSelect={vi.fn()} />);
    expect(screen.getByText("Loading themes…")).toBeInTheDocument();
    expect(screen.getByRole("combobox", { name: /choose a theme/i })).toHaveAttribute("aria-busy", "true");
  });
});
