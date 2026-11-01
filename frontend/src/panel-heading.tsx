import type { ReactNode } from "react";
import "./panel-heading.css";

export function PanelHeading({
  title,
  id,
  className = "",
  children,
}: {
  title: string;
  id?: string;
  className?: string;
  children?: ReactNode;
}) {
  return (
    <div className={`panel-heading ${className}`.trim()}>
      <h2 id={id}>{title}</h2>
      {children && <div className="panel-heading-actions">{children}</div>}
    </div>
  );
}
