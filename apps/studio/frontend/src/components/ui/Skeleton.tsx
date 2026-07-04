export interface SkeletonProps {
  /** Size the block via utility classes (h-20, w-full, …). */
  className?: string;
}

export default function Skeleton({ className }: SkeletonProps) {
  return (
    <div
      aria-hidden="true"
      className={["skeleton rounded-md", className].filter(Boolean).join(" ")}
    />
  );
}
