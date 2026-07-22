interface SparklineProps {
  values: number[];
  width?: number;
  height?: number;
  className?: string;
}

export function Sparkline({ values, width = 120, height = 36, className = "" }: SparklineProps) {
  if (values.length < 2) {
    return (
      <svg width={width} height={height} className={`sparkline ${className}`}>
        <line x1={0} y1={height / 2} x2={width} y2={height / 2} className="sparkline-flat" />
      </svg>
    );
  }

  const min = Math.min(...values);
  const max = Math.max(...values);
  const range = max - min || 1;
  const points = values
    .map((v, i) => {
      const x = (i / (values.length - 1)) * width;
      const y = height - ((v - min) / range) * (height - 4) - 2;
      return `${x},${y}`;
    })
    .join(" ");

  const up = values[values.length - 1] >= values[0];

  return (
    <svg width={width} height={height} className={`sparkline ${up ? "up" : "down"} ${className}`}>
      <polyline points={points} fill="none" strokeWidth="1.5" />
    </svg>
  );
}
