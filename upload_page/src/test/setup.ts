// Registers jest-dom's matchers (toBeInTheDocument, etc.) on vitest's
// `expect`. Imported as a real module (not just referenced as a setupFiles
// string) so the TS compiler also picks up its Assertion type augmentation.
import "@testing-library/jest-dom/vitest";
import { afterEach } from "vitest";
import { installMatchMediaStub, resetMatchMediaStub } from "./matchMedia";

// jsdom lacks matchMedia, which the color-scheme hook reads on every render.
installMatchMediaStub();
afterEach(resetMatchMediaStub);
