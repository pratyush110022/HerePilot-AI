declare namespace H {
  namespace service {
    class Platform {
      constructor(options: { apikey: string });
      createDefaultLayers(): any;
    }
  }
  namespace Map {
    class Events { constructor(...args: any[]); }
  }
  namespace geo {
    class Point { constructor(lat: number, lng: number); }
    class LineString {
      pushLatLngAlt(lat: number, lng: number, alt?: number): void;
      getPointCount(): number;
    }
    class Rect {
      constructor(left: number, top: number, right: number, bottom: number);
    }
  }
  namespace map {
    class Map {
      constructor(container: HTMLElement, layer: any, options: any);
      addObject(object: any): void;
      removeObject(object: any): void;
      getViewModel(): any;
      getViewPort(): any;
      addEventListener(name: string, handler: (...args: any[]) => void): void;
      dispose(): void;
      setCenter(point: any): void;
      setZoom(zoom: number): void;
      getViewBounds(): any;
    }
    class Group { addObjects(objects: any[]): void; removeAll(): void; }
    class Polyline { constructor(lineString: any, options?: any); }
    class DomIcon { constructor(html: string, options?: any); }
    class DomMarker { constructor(point: any, options?: any); setData(data: any): void; addEventListener(name: string, handler: (...args: any[]) => void): void; }
  }
  namespace mapevents { class Behavior { constructor(events: any); } }
  namespace ui { class UI { static createDefault(map: any, layers: any): any; addBubble(bubble: any): void; removeBubble(bubble: any): void; getBubbles(): any[]; } class InfoBubble { constructor(point: any, options: any); } }
  namespace events { class MapEvents { constructor(map: any); } }
}
