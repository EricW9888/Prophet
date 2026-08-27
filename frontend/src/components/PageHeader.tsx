import { type ReactNode } from "react";

export default function PageHeader({
  title,
  description,
  eyebrow,
  actions,
  className = "",
}: {
  title: string;
  description: string;
  eyebrow?: string;
  actions?: ReactNode;
  className?: string;
}) {
  return (
    <header
      className={`flex min-w-0 flex-col gap-4 border-b border-line pb-5 lg:flex-row lg:items-end lg:justify-between ${className}`}
    >
      <div className="min-w-0">
        {eyebrow ? (
          <p className="text-[11px] font-semibold uppercase text-muted">{eyebrow}</p>
        ) : null}
        <h1 className={`${eyebrow ? "mt-1.5" : ""} text-2xl font-semibold text-foreground sm:text-3xl`}>
          {title}
        </h1>
        <p className="mt-2 max-w-3xl text-sm leading-6 text-muted">{description}</p>
      </div>
      {actions ? (
        <div className="flex w-full flex-wrap items-center gap-2 lg:w-auto lg:shrink-0">{actions}</div>
      ) : null}
    </header>
  );
}
