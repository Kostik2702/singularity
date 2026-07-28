# Ruby is NOT a supported language (decision D6): zero .rb files exist in this
# codebase, so inventing boundaries for it would ship untested guesses. This
# fixture exists to prove the chunker falls back instead of pretending.
module Billing
  class InvoiceCalculator
    def initialize(order)
      @order = order
    end

    def total
      subtotal + tax
    end

    def subtotal
      @order.lines.sum(&:amount)
    end

    def tax
      subtotal * 0.2
    end
  end
end
