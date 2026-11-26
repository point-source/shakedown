import * as React from "react"
import { cva, type VariantProps } from "class-variance-authority"
import { Label } from "@/components/ui/label"
import { cn } from "@/lib/utils"

// shadcn/ui Field (new-york): a label, its control, a one-line description, and
// the error that names this setting.
function FieldSet({ className, ...props }: React.ComponentProps<"fieldset">) {
  return <fieldset data-slot="field-set" className={cn("flex flex-col gap-6", className)} {...props} />
}

function FieldLegend({ className, ...props }: React.ComponentProps<"legend">) {
  return <legend data-slot="field-legend" className={cn("mb-3 text-base font-semibold", className)} {...props} />
}

function FieldGroup({ className, ...props }: React.ComponentProps<"div">) {
  return <div data-slot="field-group" className={cn("group/field-group flex w-full flex-col gap-6", className)} {...props} />
}

const fieldVariants = cva("group/field flex w-full gap-3 data-[invalid=true]:text-destructive", {
  variants: {
    orientation: {
      vertical: "flex-col [&>*]:w-full [&>.sr-only]:w-auto",
      horizontal: "flex-row items-center [&>[data-slot=field-label]]:flex-auto has-[>[data-slot=field-content]]:items-start",
    },
  },
  defaultVariants: { orientation: "vertical" },
})

function Field({ className, orientation = "vertical", ...props }: React.ComponentProps<"div"> & VariantProps<typeof fieldVariants>) {
  return <div role="group" data-slot="field" data-orientation={orientation} className={cn(fieldVariants({ orientation }), className)} {...props} />
}

function FieldContent({ className, ...props }: React.ComponentProps<"div">) {
  return <div data-slot="field-content" className={cn("group/field-content flex flex-1 flex-col gap-1.5 leading-snug", className)} {...props} />
}

function FieldLabel({ className, ...props }: React.ComponentProps<typeof Label>) {
  return <Label data-slot="field-label" className={cn("group/field-label peer/field-label flex w-fit gap-2 leading-snug", className)} {...props} />
}

function FieldDescription({ className, ...props }: React.ComponentProps<"p">) {
  return (
    <p
      data-slot="field-description"
      className={cn("text-sm leading-normal font-normal text-muted-foreground group-has-[[data-orientation=horizontal]]/field:text-balance", className)}
      {...props}
    />
  )
}

function FieldError({ className, children, ...props }: React.ComponentProps<"div">) {
  if (!children) return null
  return <div role="alert" data-slot="field-error" className={cn("text-sm font-normal text-destructive", className)} {...props}>{children}</div>
}

export { Field, FieldContent, FieldDescription, FieldError, FieldGroup, FieldLabel, FieldLegend, FieldSet }
