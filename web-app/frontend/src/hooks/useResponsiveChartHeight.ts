import { useEffect, useState } from "react";

function chartHeightForWidth(width: number): number {
  if (width < 480) return 220;
  if (width < 768) return 260;
  if (width < 1024) return 300;
  if (width < 1280) return 340;
  return 380;
}

export function useResponsiveChartHeight() {
  const [height, setHeight] = useState(() =>
    chartHeightForWidth(typeof window !== "undefined" ? window.innerWidth : 1280),
  );

  useEffect(() => {
    const update = () => setHeight(chartHeightForWidth(window.innerWidth));
    update();
    window.addEventListener("resize", update);
    return () => window.removeEventListener("resize", update);
  }, []);

  return height;
}
