import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { ColorSchemeToggle } from "./ColorSchemeToggle";

afterEach(cleanup);

describe("ColorSchemeToggle", () => {
  it("offers auto, light and dark as a labelled group", () => {
    render(<ColorSchemeToggle preference="system" onChange={() => {}} />);
    expect(screen.getByRole("radiogroup", { name: "Color scheme" })).toBeInTheDocument();
    for (const name of ["Auto (match this computer)", "Light", "Dark"]) {
      expect(screen.getByRole("radio", { name })).toBeInTheDocument();
    }
  });

  it("marks the current preference as checked", () => {
    render(<ColorSchemeToggle preference="dark" onChange={() => {}} />);
    expect(screen.getByRole("radio", { name: "Dark" })).toBeChecked();
    expect(screen.getByRole("radio", { name: "Light" })).not.toBeChecked();
  });

  it("reports the chosen preference", () => {
    const onChange = vi.fn();
    render(<ColorSchemeToggle preference="system" onChange={onChange} />);
    fireEvent.click(screen.getByRole("radio", { name: "Light" }));
    expect(onChange).toHaveBeenCalledWith("light");
  });

  it("ignores a click on the already-checked option", () => {
    const onChange = vi.fn();
    render(<ColorSchemeToggle preference="dark" onChange={onChange} />);
    fireEvent.click(screen.getByRole("radio", { name: "Dark" }));
    expect(onChange).not.toHaveBeenCalled();
  });
});
