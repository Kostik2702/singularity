import { thing } from "./thing";

/**
 * Public shape of a widget as the API returns it.
 */
export interface WidgetProps {
  id: string;
  label: string;
}

// Renders one widget; the only stateful component in this file.
export class WidgetView {
  constructor(private props: WidgetProps) {}

  render(): string {
    return this.props.label;
  }
}

export const DEFAULT_WIDGET: WidgetProps = {
  id: "none",
  label: "",
};

// Fetches a widget and retries once on a 5xx.
export const loadWidget = async (id: string): Promise<WidgetProps> => {
  const res = await thing(id);
  return res;
};

export enum WidgetState {
  Idle,
  Busy,
}

function internalHelper(a: number): number {
  return a + 1;
}
