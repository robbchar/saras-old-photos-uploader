import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { RunningOutput } from "./RunningOutput";

afterEach(cleanup);

describe("RunningOutput", () => {
  it("renders a plain line verbatim", () => {
    render(
      <RunningOutput lines={["uploading lcps-photosexample-00001"]} done={0} planned={null} stopping={false} onStop={vi.fn()} />,
    );
    expect(screen.getByText("uploading lcps-photosexample-00001")).toBeInTheDocument();
  });

  it("shows only the text after the last \\r, not what it overwrote", () => {
    render(<RunningOutput lines={["a\rb"]} done={0} planned={null} stopping={false} onStop={vi.fn()} />);
    expect(screen.getByText("b")).toBeInTheDocument();
    expect(screen.queryByText("a")).not.toBeInTheDocument();
  });

  it("shows done-of-planned progress", () => {
    render(<RunningOutput lines={[]} done={12} planned={42} stopping={false} onStop={vi.fn()} />);
    expect(screen.getByText("12 of 42")).toBeInTheDocument();
  });

  it("shows a running count when planned is not yet known", () => {
    render(<RunningOutput lines={[]} done={3} planned={null} stopping={false} onStop={vi.fn()} />);
    expect(screen.getByText("3 so far")).toBeInTheDocument();
  });

  it("asks to confirm, then calls onStop once confirmed", () => {
    const onStop = vi.fn();
    render(<RunningOutput lines={[]} done={0} planned={null} stopping={false} onStop={onStop} />);

    fireEvent.click(screen.getByRole("button", { name: "Stop" }));
    expect(screen.getByText("Stop after the current photo?")).toBeInTheDocument();
    expect(onStop).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole("button", { name: "Stop" }));
    expect(onStop).toHaveBeenCalledTimes(1);
  });

  it("dismisses the confirm prompt on cancel without calling onStop", () => {
    const onStop = vi.fn();
    render(<RunningOutput lines={[]} done={0} planned={null} stopping={false} onStop={onStop} />);

    fireEvent.click(screen.getByRole("button", { name: "Stop" }));
    fireEvent.click(screen.getByRole("button", { name: "Cancel" }));

    expect(onStop).not.toHaveBeenCalled();
    expect(screen.queryByText("Stop after the current photo?")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Stop" })).toBeInTheDocument();
  });

  it("shows the stopping message and disables Stop once stopping is true", () => {
    render(<RunningOutput lines={[]} done={5} planned={10} stopping onStop={vi.fn()} />);

    const stoppingButton = screen.getByRole("button", { name: "Stopping after the current photo…" });
    expect(stoppingButton).toBeDisabled();
    expect(screen.queryByRole("button", { name: "Stop" })).not.toBeInTheDocument();
  });

  it("auto-scrolls to the bottom when new lines arrive and the reader was already there", () => {
    const { rerender } = render(
      <RunningOutput lines={["one"]} done={0} planned={null} stopping={false} onStop={vi.fn()} />,
    );
    const log = screen.getByRole("log");
    Object.defineProperty(log, "scrollHeight", { value: 500, configurable: true });
    Object.defineProperty(log, "clientHeight", { value: 500, configurable: true });
    Object.defineProperty(log, "scrollTop", { value: 0, writable: true, configurable: true });

    rerender(<RunningOutput lines={["one", "two"]} done={0} planned={null} stopping={false} onStop={vi.fn()} />);

    expect(log.scrollTop).toBe(500);
  });

  it("does not force scroll to bottom once the reader has scrolled up", () => {
    const { rerender } = render(
      <RunningOutput lines={["one"]} done={0} planned={null} stopping={false} onStop={vi.fn()} />,
    );
    const log = screen.getByRole("log");
    Object.defineProperty(log, "scrollHeight", { value: 1000, configurable: true });
    Object.defineProperty(log, "clientHeight", { value: 100, configurable: true });
    Object.defineProperty(log, "scrollTop", { value: 0, writable: true, configurable: true });

    // The reader scrolls up: far from the bottom now.
    fireEvent.scroll(log);

    // More output arrives, growing the pane - but the view must not jump.
    Object.defineProperty(log, "scrollHeight", { value: 1200, configurable: true });
    rerender(<RunningOutput lines={["one", "two"]} done={0} planned={null} stopping={false} onStop={vi.fn()} />);

    expect(log.scrollTop).toBe(0);
  });
});
