import * as React from "react"
import * as SheetPrimitive from "@radix-ui/react-dialog"
import { X } from "lucide-react"
import { cn } from "@/lib/utils"

const Sheet = SheetPrimitive.Root
const SheetTrigger = SheetPrimitive.Trigger
const SheetClose = SheetPrimitive.Close

function SheetContent({ className, children, side = "right", ...props }: React.ComponentProps<typeof SheetPrimitive.Content> & { side?: "right" | "left" }) {
  return (
    <SheetPrimitive.Portal>
      <SheetPrimitive.Overlay className="fixed inset-0 z-50 bg-black/35 backdrop-blur-[1px] data-[state=open]:animate-in data-[state=closed]:animate-out" />
      <SheetPrimitive.Content className={cn("fixed inset-y-0 z-50 flex h-full w-full max-w-lg flex-col border-l bg-background shadow-xl duration-200 data-[state=open]:animate-in data-[state=closed]:animate-out", side === "right" ? "right-0 data-[state=closed]:slide-out-to-right data-[state=open]:slide-in-from-right" : "left-0 border-l-0 border-r data-[state=closed]:slide-out-to-left data-[state=open]:slide-in-from-left", className)} {...props}>
        {children}
        <SheetPrimitive.Close className="absolute right-4 top-4 rounded-md p-1.5 text-muted-foreground hover:bg-accent hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring">
          <X className="size-4" /><span className="sr-only">关闭</span>
        </SheetPrimitive.Close>
      </SheetPrimitive.Content>
    </SheetPrimitive.Portal>
  )
}
function SheetHeader(props: React.ComponentProps<"div">) { return <div className="space-y-1.5 border-b px-6 py-5" {...props} /> }
function SheetTitle(props: React.ComponentProps<typeof SheetPrimitive.Title>) { return <SheetPrimitive.Title className="text-base font-semibold" {...props} /> }
function SheetDescription(props: React.ComponentProps<typeof SheetPrimitive.Description>) { return <SheetPrimitive.Description className="text-sm leading-6 text-muted-foreground" {...props} /> }

export { Sheet, SheetTrigger, SheetClose, SheetContent, SheetHeader, SheetTitle, SheetDescription }
