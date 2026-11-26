import * as React from "react"
import { cva, type VariantProps } from "class-variance-authority"
import { XIcon } from "lucide-react"
import { Toast as ToastPrimitive } from "radix-ui"
import { cn } from "@/lib/utils"

// shadcn/ui toast (new-york), on Radix Toast. Kit addition: the `success`,
// `warning`, and `danger` tones match the Badge and Alert tones.
const ToastProvider = ToastPrimitive.Provider

function ToastViewport({ className, ...props }: React.ComponentProps<typeof ToastPrimitive.Viewport>) {
  return (
    <ToastPrimitive.Viewport
      data-slot="toast-viewport"
      className={cn(
        "fixed top-0 z-[100] flex max-h-screen w-full flex-col-reverse p-4 sm:top-auto sm:right-0 sm:bottom-0 sm:flex-col md:max-w-[420px]",
        className
      )}
      {...props}
    />
  )
}

const toastVariants = cva(
  "group pointer-events-auto relative flex w-full items-center justify-between gap-3 overflow-hidden rounded-md border p-4 pr-8 shadow-lg",
  {
    variants: {
      variant: {
        default: "border bg-background text-foreground",
        success: "border bg-background text-foreground [&_[data-slot=toast-title]]:text-success",
        warning: "border bg-background text-foreground [&_[data-slot=toast-title]]:text-warning",
        danger: "border bg-background text-foreground [&_[data-slot=toast-title]]:text-destructive",
      },
    },
    defaultVariants: { variant: "default" },
  }
)

function Toast({
  className,
  variant,
  ...props
}: React.ComponentProps<typeof ToastPrimitive.Root> & VariantProps<typeof toastVariants>) {
  return <ToastPrimitive.Root data-slot="toast" className={cn(toastVariants({ variant }), className)} {...props} />
}

function ToastAction({ className, ...props }: React.ComponentProps<typeof ToastPrimitive.Action>) {
  return (
    <ToastPrimitive.Action
      data-slot="toast-action"
      className={cn(
        "inline-flex h-8 shrink-0 items-center justify-center rounded-md border bg-transparent px-3 text-sm font-medium transition-colors hover:bg-secondary focus:ring-1 focus:ring-ring focus:outline-none disabled:pointer-events-none disabled:opacity-50",
        className
      )}
      {...props}
    />
  )
}

function ToastClose({ className, ...props }: React.ComponentProps<typeof ToastPrimitive.Close>) {
  return (
    <ToastPrimitive.Close
      data-slot="toast-close"
      className={cn(
        "absolute top-1 right-1 rounded-md p-1 text-foreground/50 opacity-0 transition-opacity group-hover:opacity-100 hover:text-foreground focus:opacity-100 focus:ring-1 focus:outline-none",
        className
      )}
      toast-close=""
      {...props}
    >
      <XIcon className="size-4" />
    </ToastPrimitive.Close>
  )
}

function ToastTitle({ className, ...props }: React.ComponentProps<typeof ToastPrimitive.Title>) {
  return <ToastPrimitive.Title data-slot="toast-title" className={cn("text-sm font-semibold [&+div]:text-xs", className)} {...props} />
}

function ToastDescription({ className, ...props }: React.ComponentProps<typeof ToastPrimitive.Description>) {
  return <ToastPrimitive.Description data-slot="toast-description" className={cn("text-sm text-muted-foreground", className)} {...props} />
}

export { Toast, ToastAction, ToastClose, ToastDescription, ToastProvider, ToastTitle, ToastViewport, toastVariants }
