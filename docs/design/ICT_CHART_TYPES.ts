/**
 * ICT chart overlay contract.
 *
 * Time is Unix seconds (Lightweight Charts UTCTimestamp), price is the raw
 * float from the exchange K-line.  Overlays are rendered by a series primitive
 * using timeToCoordinate()/priceToCoordinate(), never by DOM pixel guesses.
 */
export type UnixSeconds = number;

export type ICTDirection = "bullish" | "bearish" | "neutral";

export interface ICTCandle {
  time: UnixSeconds;
  open: number;
  high: number;
  low: number;
  close: number;
  volume: number;
}

export interface ICTMarker {
  time: UnixSeconds;
  /** Lightweight Charts marker position. */
  position: "aboveBar" | "belowBar" | "inBar";
  shape: "circle" | "square" | "arrowUp" | "arrowDown";
  color: string;
  text: string;
  /** ICT concept: MSS, BOS, Sweep, BSP, SIG. */
  kind: "MSS" | "BOS" | "Sweep" | "BSP" | "SIG" | "SMT" | "CISD" | "PO3" | "AMD";
  direction: ICTDirection;
}

export interface ICTPriceLine {
  price: number;
  title: string;
  color: string;
  /** Lightweight Charts LineStyle: 0 solid, 2 dashed, 3 dotted. */
  lineStyle: 0 | 2 | 3;
  lineWidth: 1 | 2;
  /** Full-width levels use the current visible range; these are optional. */
  startTime?: UnixSeconds;
  endTime?: UnixSeconds;
}

export interface ICTRectangle {
  kind: "FVG" | "IFVG" | "OB" | "AsianRange" | "ChanZS";
  direction: ICTDirection;
  startTime: UnixSeconds;
  endTime: UnixSeconds;
  low: number;
  high: number;
  fill: string;
  border: string;
  label: string;
  labelColor: string;
}

export interface ICTSegment {
  startTime: UnixSeconds;
  endTime: UnixSeconds;
  startPrice: number;
  endPrice: number;
  color: string;
  width: 1 | 2;
  label: "BI" | "SEG";
}

export interface ICTData {
  markers: ICTMarker[];
  priceLines: ICTPriceLine[];
  rectangles: ICTRectangle[];
  segments: ICTSegment[];
}
