import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { StartDialog } from "./StartDialog";

afterEach(cleanup);

function openDialog() {
  fireEvent.click(screen.getByRole("button", { name: "Upload 3 photos to Internet Archive" }));
}

describe("StartDialog", () => {
  it("shows a trigger labeled with the exact count and destination", () => {
    render(
      <StartDialog count={3} batch="Fishing" live={false} onConfirm={vi.fn()} onCancel={vi.fn()} />,
    );
    expect(
      screen.getByRole("button", { name: "Upload 3 photos to Internet Archive" }),
    ).toBeInTheDocument();
  });

  it("restates the theme and count once opened", () => {
    render(
      <StartDialog count={3} batch="Fishing" live={false} onConfirm={vi.fn()} onCancel={vi.fn()} />,
    );
    openDialog();
    const dialog = within(screen.getByRole("dialog"));
    expect(dialog.getByText(/Fishing/)).toBeInTheDocument();
    expect(dialog.getByText(/\b3\b/)).toBeInTheDocument();
  });

  it("adds the cannot-be-undone-or-renamed warning in live mode", () => {
    render(
      <StartDialog count={3} batch="Fishing" live onConfirm={vi.fn()} onCancel={vi.fn()} />,
    );
    openDialog();
    expect(screen.getByText(/cannot be undone or renamed/i)).toBeInTheDocument();
  });

  it("does not show the live-only warning in test mode", () => {
    render(
      <StartDialog count={3} batch="Fishing" live={false} onConfirm={vi.fn()} onCancel={vi.fn()} />,
    );
    openDialog();
    expect(screen.queryByText(/cannot be undone or renamed/i)).not.toBeInTheDocument();
  });

  it("calls onConfirm when Confirm is clicked", () => {
    const onConfirm = vi.fn();
    render(
      <StartDialog count={3} batch="Fishing" live={false} onConfirm={onConfirm} onCancel={vi.fn()} />,
    );
    openDialog();
    fireEvent.click(screen.getByRole("button", { name: "Confirm" }));
    expect(onConfirm).toHaveBeenCalledTimes(1);
  });

  it("calls onCancel when Cancel is clicked", () => {
    const onCancel = vi.fn();
    render(
      <StartDialog count={3} batch="Fishing" live={false} onConfirm={vi.fn()} onCancel={onCancel} />,
    );
    openDialog();
    fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
    expect(onCancel).toHaveBeenCalledTimes(1);
  });
});
